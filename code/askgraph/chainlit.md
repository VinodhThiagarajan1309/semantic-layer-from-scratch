# askgraph

Ask business questions in plain English. The agent writes SPARQL, runs it against the
Stardog `kit-c360` knowledge graph (Aurora + Redshift virtual graphs) and answers from the rows.

- Open each **SPARQL** step to see the exact query, row count, time and the first rows.
- You are logged in as a Stardog user: `hr_user` sees the PII graph, `marketing_user` does not.
  Log out (avatar, top right) to switch users.
- Use the **settings** (gear icon) to switch the model or the reasoning schema. Changes apply
  to the next message.
- Past chats are in the left sidebar; reopen one to continue it.
