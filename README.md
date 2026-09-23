# The Semantic Layer, From Scratch

A hands-on, browser-readable guide to building a semantic layer with Stardog over Amazon Aurora PostgreSQL and Amazon Redshift, with an LLM agent on top. Open `index.html`, or read it online via GitHub Pages.

- Follows the AWS Machine Learning blog post "Build a semantic layer for agentic AI on AWS with Stardog and Amazon Bedrock AgentCore".
- Uses Stardog's c360 Knowledge Kit (https://github.com/stardog-union/knowledge-kits/tree/main/examples/c360). All customer names, emails, SSNs and card numbers are from that kit's fictional training dataset.
- Endpoints, users and hostnames are placeholders (`<your-endpoint>`, `stardog_admin`). To follow along you need your own Stardog Cloud endpoint and AWS resources. The sample agents (`agent.py`, `agent_v2.py`, `Agent.java`) read all credentials from environment variables.
