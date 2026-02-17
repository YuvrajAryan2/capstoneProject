import json
import os
import uuid
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3

TABLE_NAME       = os.environ.get("FEEDBACK_TABLE_NAME", "FeedbackSubmissions")
EXPORT_BUCKET    = os.environ.get("EXPORT_BUCKET_NAME")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
BEDROCK_REGION   = os.environ.get("BEDROCK_REGION", "ca-central-1")
AI_PROVIDER      = os.environ.get("AI_PROVIDER", "bedrock").strip().lower()

dynamodb   = boto3.resource("dynamodb", region_name=BEDROCK_REGION)
table      = dynamodb.Table(TABLE_NAME)
bedrock    = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
comprehend = boto3.client("comprehend", region_name=BEDROCK_REGION)
s3         = boto3.client("s3", region_name=BEDROCK_REGION) if EXPORT_BUCKET else None


def _cors() -> Dict[str, str]:
    return {
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    method = event.get("httpMethod", "")
    path   = event.get("path", "")
    if method == "OPTIONS":
        return {"statusCode": 200, "headers": _cors(), "body": ""}
    try:
        if path.endswith("/feedback") and method == "POST":
            return handle_post_feedback(event)
        if path.endswith("/insights") and method == "GET":
            return handle_get_insights()
        return {"statusCode": 404, "headers": _cors(), "body": json.dumps({"message": "Not Found"})}
    except Exception as exc:
        print("ERROR:", str(exc))
        traceback.print_exc()
        raise


def handle_post_feedback(event: Dict[str, Any]) -> Dict[str, Any]:
    body = json.loads(event.get("body") or "{}")

    reviewer_name  = (body.get("name")         or "").strip()
    reviewer_email = (body.get("email")        or "").strip()
    employee_name  = (body.get("employeeName") or "").strip()
    department     = (body.get("department")   or "").strip()
    review_period  = (body.get("reviewPeriod") or "").strip()
    rating         = body.get("rating", 0)
    message        = (body.get("message")      or "").strip()

    if not reviewer_name or not reviewer_email or not employee_name or not message:
        return {
            "statusCode": 400, "headers": _cors(),
            "body": json.dumps({"message": "name, email, employeeName and message are required"}),
        }

    feedback_id = str(uuid.uuid4())
    timestamp   = datetime.now(timezone.utc).isoformat()

    item = {
        "feedbackId":   feedback_id,
        "name":         reviewer_name,
        "email":        reviewer_email,
        "employeeName": employee_name,
        "department":   department,
        "reviewPeriod": review_period,
        "rating":       int(rating) if rating else 0,
        "message":      message,
        "sentiment":    None,
        "topics":       [],
        "summary":      None,
        "timestamp":    timestamp,
    }
    table.put_item(Item=item)

    try:
        ai = call_ai_analysis(message, employee_name)
        table.update_item(
            Key={"feedbackId": feedback_id},
            UpdateExpression="SET sentiment = :s, topics = :t, summary = :m",
            ExpressionAttributeValues={":s": ai["sentiment"], ":t": ai["topics"], ":m": ai["summary"]},
        )
        if s3 and EXPORT_BUCKET:
            s3.put_object(
                Bucket=EXPORT_BUCKET,
                Key=f"exports/{timestamp[:7]}/{feedback_id}.json",
                Body=json.dumps({**item, **ai}),
                ContentType="application/json",
            )
    except Exception as exc:
        print("AI ERROR:", str(exc))
        traceback.print_exc()

    return {"statusCode": 201, "headers": _cors(), "body": json.dumps({"feedbackId": feedback_id})}


def handle_get_insights() -> Dict[str, Any]:
    response = table.scan()
    items: List[Dict[str, Any]] = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    total            = len(items)
    sentiment_counts = {"positive": 0, "negative": 0, "neutral": 0}
    summaries: List[str]            = []
    topics:    List[str]            = []
    reviews:   List[Dict[str, Any]] = []

    for item in sorted(items, key=lambda x: x.get("timestamp", ""), reverse=True):
        sent = (item.get("sentiment") or "neutral").lower()
        if sent in sentiment_counts:
            sentiment_counts[sent] += 1
        if item.get("summary"):
            summaries.append(item["summary"])
        if isinstance(item.get("topics"), list):
            topics.extend(item["topics"])
        reviews.append({
            "employeeName": item.get("employeeName") or item.get("name") or "Unknown",
            "department":   item.get("department",   ""),
            "reviewPeriod": item.get("reviewPeriod", ""),
            "rating":       int(item.get("rating", 0) or 0),
            "sentiment":    sent,
            "summary":      item.get("summary") or "",
            "timestamp":    item.get("timestamp", ""),
        })

    return {
        "statusCode": 200, "headers": _cors(),
        "body": json.dumps({
            "totalSubmissions": total,
            "sentimentCounts":  sentiment_counts,
            "summaries":        summaries,
            "topics":           topics,
            "reviews":          reviews,
        }),
    }


def call_ai_analysis(message: str, employee_name: str = "") -> Dict[str, Any]:
    if AI_PROVIDER == "comprehend":
        return call_comprehend_analysis(message)
    return call_bedrock_analysis(message, employee_name)


def call_comprehend_analysis(message: str) -> Dict[str, Any]:
    text      = message.strip()[:5000]
    sent_resp = comprehend.detect_sentiment(Text=text, LanguageCode="en")
    sent_map  = {"POSITIVE": "positive", "NEGATIVE": "negative", "NEUTRAL": "neutral", "MIXED": "neutral"}
    sentiment = sent_map.get(sent_resp.get("Sentiment", "NEUTRAL"), "neutral")
    kp_resp   = comprehend.detect_key_phrases(Text=text, LanguageCode="en")
    topics    = [p["Text"] for p in kp_resp.get("KeyPhrases", []) if p.get("Text")]
    summary   = f"Key themes: {', '.join(topics[:5])}" if topics else "No key themes detected."
    return {"sentiment": sentiment, "topics": topics, "summary": summary}


def call_bedrock_analysis(message: str, employee_name: str = "") -> Dict[str, Any]:
    name_hint = f" about {employee_name}" if employee_name else ""
    prompt = f"""You are an expert HR analyst reviewing employee performance feedback{name_hint}.

Analyze the feedback below and return ONLY valid JSON — no markdown, no code fences, no extra text.

Required JSON shape:
{{
  "sentiment": "<positive | negative | neutral>",
  "topics": ["<topic1>", "<topic2>", ...],
  "summary": "<2-3 sentence insight>"
}}

Rules for "summary":
- Sentence 1: State the overall performance verdict.{f" Refer to the employee as {employee_name}." if employee_name else ""}
- Sentence 2: Call out the most notable strength with a concrete example from the feedback.
- Sentence 3 (if warranted): Name the single most important area for improvement, framed constructively.
- Plain business English. Vary your opening — do not always start with "The employee".
- Keep total length under 60 words.

Rules for "topics":
- 3–8 specific skill/theme tags as short lowercase noun phrases (e.g. "code quality", "communication").

Rules for "sentiment":
- "positive" if praise outweighs criticism. "negative" if concerns outweigh praise. "neutral" if balanced.

Feedback:
\"\"\"{message.strip()}\"\"\"
"""
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512, "temperature": 0,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }
    resp      = bedrock.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps(body), contentType="application/json", accept="application/json")
    resp_body = json.loads(resp["body"].read())
    raw       = "".join(c.get("text", "") for c in resp_body.get("content", []) if c.get("type") == "text").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw    = raw.strip()
    parsed = json.loads(raw)
    return {"sentiment": parsed.get("sentiment", "neutral"), "topics": parsed.get("topics", []), "summary": parsed.get("summary", "")}
