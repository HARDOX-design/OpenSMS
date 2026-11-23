import json
from fastapi import FastAPI, Request

app = FastAPI()


@app.post("/simple-hook")
async def debug_webhook(request: Request):
    print("=" * 50)
    print("HEADERS:")
    for key, value in request.headers.items():
        print(f"  {key}: {value}")

    print("-" * 50)
    raw_body = await request.body()
    text_body = raw_body.decode("utf-8", errors="replace")

    print("BODY:")
    try:
        # Pretty-print if it's valid JSON
        parsed_json = json.loads(text_body)
        print(json.dumps(parsed_json, indent=2))
    except json.JSONDecodeError:
        # Otherwise print raw text (e.g. form-urlencoded or plain string)
        print(text_body)

    print("=" * 50)

    return {"status": "ok"}