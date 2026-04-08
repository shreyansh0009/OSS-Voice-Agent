Squad Selection : 
1. Create squad.js in config/squads/demo_squad.json
2. .env -> set path of your squad on this variable : SQUAD_PATH=config/squads/demo_squad.json

Welcome message : 
3. .env -> set welcome message on this variable : WELCOME_MESSAGE=Hello! Welcome to Greaves Mobility Customer Care. I'm Naina — how may I assist you today? 

AFter this need to run this command :
4. rm -rf chroma_db
5. python -m providers.rag.ingest --collection crml_company --files knowledge_bases/new_knowledgebase.md (This is for Service agent)
6. python -m providers.rag.ingest --collection godrej_appliances_products --files knowledge_bases/new2_appliances.md (This is for Sales agent)


