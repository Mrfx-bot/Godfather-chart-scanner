"""Chart screenshot analyzer.

  pip install -r requirements.txt
  set ANTHROPIC_API_KEY=your_key      (Windows)   /  export ANTHROPIC_API_KEY=...  (Mac/Linux)
  uvicorn app:app --reload --host 0.0.0.0 --port 8000
Then open http://localhost:8000 (or http://<your-pc-ip>:8000 from your phone on the same wifi).
"""
import base64
import json
import os
import re
from pathlib import Path

import anthropic
from fastapi import FastAPI, File, Form, Header, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_BYTES = 5 * 1024 * 1024
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
app = FastAPI()
HERE = Path(__file__).parent
ACCESS_CODE = os.getenv("APP_PASSWORD", "")  # set this when hosting online so strangers can't use your API credit
STATIC = {"/manifest.json": "application/manifest+json", "/sw.js": "application/javascript",
          "/icon-192.png": "image/png", "/icon-512.png": "image/png", "/icon-180.png": "image/png"}


def _static(name: str):
    return lambda: FileResponse(HERE / name.lstrip("/"), media_type=STATIC[name])


for _n in STATIC:
    app.get(_n)(_static(_n))

STRATEGIES = {
    "auto": "Pick whichever of the strategies below fits the chart best.",
    "smc": "Smart Money Concepts: BOS/CHoCH, liquidity sweeps, order blocks, fair value gaps, premium/discount.",
    "break_retest": "Break and retest of a clear support/resistance level.",
    "silver_bullet": "ICT Silver Bullet: liquidity sweep, market structure shift, entry in a fair value gap during a kill zone.",
    "range": "Range trading between clear support and resistance, fading the edges.",
    "fib": "Fibonacci retracement (50%, 61.8%, 78.6%) of the latest impulse leg with a reversal confirmation.",
}

SYSTEM = """You are a disciplined price-action analyst reading a screenshot of a trading chart.

Rules:
- Read prices ONLY from the chart's price axis labels and the visible candles. Never invent price levels.
- If the price axis is not legible, or there is no clean setup, return "no_trade". Passing on a bad chart is a correct answer.
- Every price you give must be consistent with the axis. Buy: sl < entry < tp. Sell: tp < entry < sl.
- Put the stop-loss at a structural invalidation point (beyond the swing / zone that would prove the idea wrong), not an arbitrary distance.
- Be honest about uncertainty. Do not promise outcomes. Confidence is low/medium/high.
- Reasons must reference things actually visible on the chart.

Respond with ONLY a JSON object, no markdown fences:
{
 "decision": "buy" | "sell" | "no_trade",
 "strategy_used": string,
 "symbol_timeframe_read": string,
 "entry": number|null, "sl": number|null, "tp": number|null,
 "confidence": "low"|"medium"|"high",
 "reasons": [string, ...],
 "invalidation": string,
 "risks": [string, ...],
 "axis_readable": boolean
}"""


def validate(plan: dict) -> dict:
    """Sanity-check the model's numbers so a misread never reaches the user as a real trade."""
    d = plan.get("decision")
    if d in ("buy", "sell"):
        try:
            e, s, t = float(plan["entry"]), float(plan["sl"]), float(plan["tp"])
        except (TypeError, ValueError, KeyError):
            e = s = t = None
        ok = e is not None and (
            (d == "buy" and s < e < t) or (d == "sell" and t < e < s)
        )
        if not ok:
            plan["risks"] = plan.get("risks", []) + [
                "Model returned inconsistent levels, so the trade was discarded."
            ]
            plan["decision"] = "no_trade"
            plan["entry"] = plan["sl"] = plan["tp"] = None
        else:
            plan["rr"] = round(abs(t - e) / abs(e - s), 2)
    return plan


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


@app.post("/analyze")
async def analyze(
    image: UploadFile = File(...),
    symbol: str = Form(""),
    timeframe: str = Form(""),
    strategy: str = Form("auto"),
    notes: str = Form(""),
    x_access_code: str = Header(""),
):
    if ACCESS_CODE and x_access_code != ACCESS_CODE:
        return JSONResponse({"error": "Wrong or missing access code."}, status_code=401)
    data = await image.read()
    if len(data) > MAX_BYTES:
        return JSONResponse({"error": "Image too large (max 5 MB)."}, status_code=413)
    media = image.content_type if image.content_type in (
        "image/png", "image/jpeg", "image/webp", "image/gif") else "image/png"

    ctx = f"Symbol: {symbol or 'read from chart'}. Timeframe: {timeframe or 'read from chart'}.\n"
    ctx += f"Strategy focus: {STRATEGIES.get(strategy, STRATEGIES['auto'])}\n"
    if notes:
        ctx += f"Trader notes: {notes}\n"

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media,
                        "data": base64.b64encode(data).decode()}},
                    {"type": "text", "text": ctx + "Analyze this chart and return the JSON."},
                ],
            }],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        plan = validate(json.loads(text))
        return plan
    except json.JSONDecodeError:
        return JSONResponse({"error": "Could not parse the analysis. Try a clearer screenshot."}, status_code=502)
    except Exception as e:  # network, auth, rate limit
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
