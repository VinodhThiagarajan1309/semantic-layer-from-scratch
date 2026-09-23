//JAVA 21
//DEPS com.openai:openai-java:4.68.0
// Agent.java - the Chapter 14 agent in Java: question -> LLM (OpenAI or OpenRouter) -> SPARQL on Stardog -> answer.
// Run:  jbang Agent.java -v "Who are our top spenders in Wisconsin?"   (or build with Maven/Gradle, see the book)
// Env:  STARDOG_ENDPOINT, STARDOG_USER, STARDOG_PASSWORD, [STARDOG_DB], [STARDOG_SCHEMA], [LLM_PROVIDER=openrouter],
//       OPENAI_API_KEY or OPENROUTER_API_KEY, [AGENT_MODEL].  Never hardcode secrets.
import com.fasterxml.jackson.annotation.JsonClassDescription;
import com.fasterxml.jackson.annotation.JsonPropertyDescription;
import com.fasterxml.jackson.annotation.JsonTypeName;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.openai.client.OpenAIClient;
import com.openai.client.okhttp.OpenAIOkHttpClient;
import com.openai.core.JsonSchemaLocalValidation;
import com.openai.models.chat.completions.*;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class Agent {
    static final int MAX_ROWS = 100, MAX_CHARS = 8000, MAX_STEPS = 8, TIMEOUT_S = 90;
    static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
    static final ObjectMapper JSON = new ObjectMapper();
    static String endpoint, db, schema, auth;
    static boolean verbose;

    static final String SYSTEM_PROMPT = """
You answer business questions about customers and purchases by writing
SPARQL for a Stardog knowledge graph (database kit-c360) and calling the run_sparql tool.
Never guess numbers: every figure in your answer must come from a tool result.

PREFIX : <tag:stardog:api:ecomm:>      (also declare xsd: and rdfs: when you use them)

Data lives in three virtual graphs (live SQL underneath). Always wrap patterns in GRAPH:
  GRAPH <virtual://aurora_c360_safe>
    :Customer  :name :firstName :lastName :email :phone, :address -> :Address, :hasRewards -> :RewardsAccount
    :Address   :streetAddress :city :state :zipCode   (state is the full name, e.g. "Wisconsin")
    :CreditCard :cardType, :cardHolder -> :Customer   (no card numbers here)
    :RewardsAccount :accountId :openDate (xsd:date)
  GRAPH <virtual://redshift_c360>
    :Order  :purchasedBy -> :Customer, :itemPurchased -> :Product, :cardUsed -> :CreditCard,
            :rewardsAccountUsed -> :RewardsAccount, :purchasePrice (xsd:decimal, unit price),
            :quantity (xsd:integer), :purchaseDate (xsd:date), :purchaseTime (string)
    :Product :name :description, :inCategory -> :Category, :vendor -> :Vendor, :msrp -> :UnitPrice
    :UnitPrice :hasPrice (xsd:float) :hasCurrency     :Category :name     :Vendor :name, :industry -> :Industry
  GRAPH <virtual://aurora_c360_pii>  :ssn, :cardNumber. Sensitive: do NOT query unless explicitly asked.
Call get_ontology if you need labels, comments or a term not listed here.

Customer IRIs are identical in both warehouses, so a variable shared by two GRAPH blocks IS
the join. Derived classes (need reasoning=true): :Big_Spender, :Large_Order, :2022_Order,
:2022_Orderer, :Sports_Category_Shopper. Leave reasoning false otherwise (it is slower).
A reasoning query must use FROM <virtual://aurora_c360_safe> FROM <virtual://redshift_c360>
instead of GRAPH blocks, so the rules can join across both warehouses.

Example. "Top 3 customers by spend, with state":
SELECT ?name ?state (SUM(?p * ?q) AS ?spend) WHERE {
  GRAPH <virtual://aurora_c360_safe> { ?c :name ?name ; :address ?a . ?a :state ?state }
  GRAPH <virtual://redshift_c360> { ?o :purchasedBy ?c ; :purchasePrice ?p ; :quantity ?q }
} GROUP BY ?name ?state ORDER BY DESC(?spend) LIMIT 3

Rules: read-only SELECT/ASK only; always add LIMIT (<= 100); spend = SUM(price * quantity);
compare dates with "2022-01-01"^^xsd:date; cast with xsd:decimal()/xsd:integer() if a comparison
misbehaves; return names, not IRIs. If a query errors or returns nothing, read the result, fix
the query and retry. Answer concisely, show the key numbers, and say which data you used.
""";

    static final String ONTOLOGY_QUERY = """
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX so: <https://schema.org/>
SELECT ?term ?kind (SAMPLE(?l) AS ?label) (SAMPLE(?cm) AS ?comment)
       (GROUP_CONCAT(DISTINCT COALESCE(STR(?sup), "")) AS ?parent)
       (GROUP_CONCAT(DISTINCT COALESCE(STR(?d), "")) AS ?domain)
       (GROUP_CONCAT(DISTINCT COALESCE(STR(?r), "")) AS ?range)
WHERE {
  VALUES ?g { <urn:stardog:training-c360:1.0:model> <urn:stardog:c360_extended_rules:1.0:model> }
  VALUES ?kind { owl:Class owl:ObjectProperty owl:DatatypeProperty }
  GRAPH ?g { ?term a ?kind .
    OPTIONAL { ?term rdfs:label ?l }  OPTIONAL { ?term rdfs:comment ?cm }  OPTIONAL { ?term rdfs:subClassOf ?sup }
    OPTIONAL { ?term so:domainIncludes ?d }  OPTIONAL { ?term so:rangeIncludes ?r } }
} GROUP BY ?term ?kind ORDER BY ?kind ?term
""";

    // The tools. The SDK derives each tool's JSON Schema from the class; the model fills the fields.
    @JsonTypeName("run_sparql")
    @JsonClassDescription("Run one read-only SPARQL SELECT or ASK query against the c360 knowledge graph.")
    static class RunSparql {
        @JsonPropertyDescription("A complete SPARQL query with PREFIX lines.")
        public String query;
        @JsonPropertyDescription("true only for derived classes such as :Big_Spender.")
        public boolean reasoning;
    }

    @JsonTypeName("get_ontology")
    @JsonClassDescription("Return every class and property in the c360 ontology with its label, comment, domain and range.")
    static class GetOntology {}

    static final Pattern FORBIDDEN = Pattern.compile("\\b(INSERT|DELETE|DROP|CLEAR|LOAD|CREATE|ADD|MOVE|COPY)\\b", Pattern.CASE_INSENSITIVE);
    static final Pattern READ_FORM = Pattern.compile("^\\s*((PREFIX|BASE)\\s[^\\n]*\\n\\s*)*(SELECT|ASK)\\b", Pattern.CASE_INSENSITIVE);
    static final Pattern TRAILING_LIMIT = Pattern.compile("\\bLIMIT\\s+(\\d+)\\s*(OFFSET\\s+\\d+\\s*)?$", Pattern.CASE_INSENSITIVE);
    static final Pattern SELECT = Pattern.compile("\\bSELECT\\b", Pattern.CASE_INSENSITIVE);

    /** Reject anything that is not a plain read, and make sure a SELECT has a sane LIMIT. */
    static String guard(String query) {
        String body = query.replaceAll("(?m)^\\s*#.*$", "").strip();
        String check = body.replaceAll("<[^<>\\s]*>|\"[^\"]*\"|'[^']*'", " ").replaceAll("#[^\\n]*", " ");
        if (FORBIDDEN.matcher(check).find())
            throw new IllegalArgumentException("Rejected: only read-only SELECT/ASK queries are allowed.");
        if (!READ_FORM.matcher(check).lookingAt())
            throw new IllegalArgumentException("Rejected: the query must be a SELECT or ASK (after PREFIX lines).");
        if (SELECT.matcher(check).find()) {
            Matcher m = TRAILING_LIMIT.matcher(check.stripTrailing());
            if (!m.find()) body += "\nLIMIT " + MAX_ROWS;
            else if (Integer.parseInt(m.group(1)) > MAX_ROWS)
                body = body.replaceFirst("(?is)(.*\\bLIMIT\\s+)\\d+", "$1" + MAX_ROWS);
        }
        return body;
    }

    /** POST one query to the read-only query endpoint; return the SPARQL JSON results. */
    static JsonNode stardog(String query, boolean reasoning) throws Exception {
        String form = "query=" + URLEncoder.encode(query, StandardCharsets.UTF_8)
                + "&reasoning=" + reasoning + "&timeout=" + (TIMEOUT_S * 1000)
                // the Chapter 10 rules; the default schema is empty
                + (reasoning ? "&schema=" + URLEncoder.encode(schema, StandardCharsets.UTF_8) : "");
        HttpRequest req = HttpRequest.newBuilder(URI.create(endpoint + "/" + db + "/query"))
                .timeout(Duration.ofSeconds(TIMEOUT_S + 10))
                .header("Authorization", auth)
                .header("Accept", "application/sparql-results+json")
                .header("Content-Type", "application/x-www-form-urlencoded")
                .POST(HttpRequest.BodyPublishers.ofString(form)).build();
        HttpResponse<String> res = HTTP.send(req, HttpResponse.BodyHandlers.ofString());
        if (res.statusCode() != 200) {
            String b = res.body();
            throw new IllegalStateException("Stardog error " + res.statusCode() + ": " + b.substring(0, Math.min(b.length(), 1500)));
        }
        return JSON.readTree(res.body());
    }

    static List<Map<String, String>> rows(JsonNode res) {
        List<Map<String, String>> rows = new ArrayList<>();
        for (JsonNode b : res.path("results").path("bindings")) {
            Map<String, String> row = new LinkedHashMap<>();
            b.fields().forEachRemaining(e -> row.put(e.getKey(), e.getValue().path("value").asText()));
            rows.add(row);
        }
        return rows;
    }

    /** Tool 1. Guard, run, flatten, truncate. Errors come back as text the model can react to. */
    static String runSparql(String query, boolean reasoning) {
        try {
            String q = guard(query == null ? "" : query);
            if (verbose) System.err.println("\n--- SPARQL (reasoning=" + reasoning + ") ---\n" + q);
            JsonNode res = stardog(q, reasoning);
            if (res.has("boolean")) return "{\"ask\": " + res.get("boolean").asBoolean() + "}";
            List<Map<String, String>> rows = rows(res);
            if (verbose) System.err.println("--- " + rows.size() + " row(s) ---");
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("columns", res.path("head").path("vars"));
            out.put("rows", rows.subList(0, Math.min(rows.size(), MAX_ROWS)));
            out.put("row_count", rows.size());
            String text = JSON.writeValueAsString(out);
            return text.length() <= MAX_CHARS ? text : text.substring(0, MAX_CHARS) + " ... [truncated]";
        } catch (Exception e) {
            return e.getMessage();
        }
    }

    /** Tool 2. Read the T-Box from the model graphs (no warehouse involved) as compact lines. */
    static String getOntology() {
        try {
            StringBuilder sb = new StringBuilder();
            for (Map<String, String> v : rows(stardog(ONTOLOGY_QUERY, false))) {
                v.replaceAll((k, s) -> s.replace("tag:stardog:api:ecomm:", ":")
                        .replace("http://www.w3.org/2001/XMLSchema#", "xsd:")
                        .replace("http://www.w3.org/2002/07/owl#", "").strip());
                sb.append(v.get("term")).append(" (").append(v.get("kind")).append(") \"")
                  .append(v.getOrDefault("label", "")).append('"');
                if (!v.getOrDefault("parent", "").isEmpty()) sb.append(" subClassOf ").append(v.get("parent"));
                if (!v.getOrDefault("domain", "").isEmpty())
                    sb.append(' ').append(v.get("domain")).append(" -> ").append(v.getOrDefault("range", ""));
                if (!v.getOrDefault("comment", "").isEmpty()) sb.append(" -- ").append(v.get("comment"));
                sb.append('\n');
            }
            return sb.toString();
        } catch (Exception e) {
            return e.getMessage();
        }
    }

    /** The agent loop: call the model, run the tools it asks for, repeat until it answers. */
    static String ask(OpenAIClient client, String model, String question) {
        ChatCompletionCreateParams.Builder params = ChatCompletionCreateParams.builder()
                .model(model)
                .addTool(RunSparql.class)
                .addTool(GetOntology.class, JsonSchemaLocalValidation.NO)
                .addSystemMessage(SYSTEM_PROMPT)
                .addUserMessage(question);
        for (int step = 0; step < MAX_STEPS; step++) {
            ChatCompletionMessage msg = client.chat().completions().create(params.build()).choices().get(0).message();
            List<ChatCompletionMessageToolCall> calls = msg.toolCalls().orElse(List.of());
            if (calls.isEmpty()) return msg.content().orElse("(no answer)");
            params.addMessage(msg);
            for (ChatCompletionMessageToolCall call : calls) {
                ChatCompletionMessageFunctionToolCall fc = call.asFunction();
                String result;
                try {
                    result = switch (fc.function().name()) {
                        case "get_ontology" -> getOntology();
                        case "run_sparql" -> {
                            RunSparql a = fc.function().arguments(RunSparql.class);
                            yield runSparql(a.query, a.reasoning);
                        }
                        default -> "Error: unknown tool " + fc.function().name();
                    };
                } catch (RuntimeException e) {
                    result = "Error: tool arguments were not valid: " + e.getMessage();
                }
                params.addMessage(ChatCompletionToolMessageParam.builder().toolCallId(fc.id()).content(result).build());
            }
        }
        return "Stopped: too many tool calls without a final answer. Try rephrasing the question.";
    }

    static String env(String key) {
        String v = System.getenv(key);
        if (v == null || v.isBlank()) throw new IllegalStateException("Please set " + key);
        return v;
    }

    public static void main(String[] args) {
        endpoint = env("STARDOG_ENDPOINT").replaceAll("/+$", "");
        db = System.getenv().getOrDefault("STARDOG_DB", "kit-c360");
        schema = System.getenv().getOrDefault("STARDOG_SCHEMA", "c360_extended_rules");
        auth = "Basic " + Base64.getEncoder().encodeToString(
                (env("STARDOG_USER") + ":" + env("STARDOG_PASSWORD")).getBytes(StandardCharsets.UTF_8));
        verbose = Arrays.asList(args).contains("-v");

        boolean openRouter = "openrouter".equalsIgnoreCase(System.getenv().getOrDefault("LLM_PROVIDER", "openai"));
        OpenAIClient client = OpenAIOkHttpClient.builder()
                .apiKey(env(openRouter ? "OPENROUTER_API_KEY" : "OPENAI_API_KEY"))
                .baseUrl(openRouter ? "https://openrouter.ai/api/v1" : "https://api.openai.com/v1")
                .build();
        String model = System.getenv().getOrDefault("AGENT_MODEL", openRouter ? "anthropic/claude-sonnet-4.6" : "gpt-4.1");

        String question = String.join(" ", Arrays.stream(args).filter(a -> !a.equals("-v")).toList());
        if (!question.isBlank()) {
            System.out.println(ask(client, model, question));
            return;
        }
        System.out.println("c360 agent (" + model + "). Ask a question; press Enter on an empty line to quit.");
        Scanner in = new Scanner(System.in);
        while (true) {
            System.out.print("\n> ");
            if (!in.hasNextLine()) break;
            String q = in.nextLine().strip();
            if (q.isEmpty()) break;
            System.out.println(ask(client, model, q));
        }
    }
}
