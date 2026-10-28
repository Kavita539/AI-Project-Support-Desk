from fastapi import FastAPI

app = FastAPI(title="AI Project Support Desk")


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
