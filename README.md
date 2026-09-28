# MediQueue Backend

FastAPI service deployed on Vercel with the explicit entrypoint
`backend.main:app`.

## Endpoints

- `/` - service information and documentation links
- `/docs` - Swagger UI
- `/redoc` - ReDoc
- `/openapi.json` - generated OpenAPI contract
- `/health` - liveness
- `/health/ready` - database readiness
