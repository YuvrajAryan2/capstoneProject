import json
import os
import uuid
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3
from botocore.exceptions import BotoCoreError, ClientError

TABLE_NAME = os.environ.get("FEEDBACK_TABLE_NAME", "FeedbackSubmissions")
EXPORT_BUCKET = os.environ.get("EXPORT_BUCKET_NAME")
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"
)
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "ca-central-1")
AI_PROVIDER = os.environ.get("AI_PROVIDER", "bedrock").strip().lower()

dynamodb = boto3.resource("dynamodb", region_name=BEDROCK_REGION)
table = dynamodb.Table(TABLE_NAME)

bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
comprehend = boto3.client("comprehend", region_name=BEDROCK_REGION)

s3 = boto3.client("s3", region_name=BEDROCK_REGION) if EXPORT_BUCKET else None


def _cors_headers() -> Dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    method = event.get("httpMethod", "")
    path = event.get("path", "")

    if method == "OPTIONS":
        return {
            "statusCode": 200,
            "headers": _cors_headers(),
            "body": ""
        }

    try:
        if path.endswith("/feedback") and method == "POST":
            return handle_post_feedback(event)

        if path.endswith("/insights") and method == "GET":
            return handle_get_insights()

        return {
            "statusCode": 404,
            "headers": _cors_headers(),
            "body": json.dumps({"message": "Not Found"}),
        }

    except Exception as exc:
        print("FULL ERROR:", str(exc))
        traceback.print_exc()
        raise  # let CloudWatch show the real error


def handle_post_feedback(event: Dict[str, Any]) -> Dict[str, Any]:
    body_raw = event.get("body") or "{}"

    body = json.loads(body_raw)

    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    message = (body.get("message") or "").strip()

    if not name or not email or not message:
        return {
            "statusCode": 400,
            "headers": _cors_headers(),
            "body": json.dumps({"message": "name, email and message are required"}),
        }

    feedback_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    item = {
        "feedbackId": feedback_id,
        "name": name,
        "email": email,
        "message": message,
        "sentiment": None,
        "topics": [],
        "summary": None,
        "timestamp": timestamp,
    }

    table.put_item(Item=item)

    try:
        ai_result = call_ai_analysis(message)

        table.update_item(
            Key={"feedbackId": feedback_id},
            UpdateExpression="SET sentiment = :s, topics = :t, summary = :m",
            ExpressionAttributeValues={
                ":s": ai_result.get("sentiment"),
                ":t": ai_result.get("topics"),
                ":m": ai_result.get("summary"),
            },
        )

        if s3 and EXPORT_BUCKET:
            year_month = timestamp[:7]
            key = f"exports/{year_month}/{feedback_id}.json"

            s3.put_object(
                Bucket=EXPORT_BUCKET,
                Key=key,
                Body=json.dumps({**item, **ai_result}),
                ContentType="application/json",
            )

    except Exception as exc:
        print("AI ERROR:", str(exc))
        traceback.print_exc()

    return {
        "statusCode": 201,
        "headers": _cors_headers(),
        "body": json.dumps({"feedbackId": feedback_id}),
    }


def handle_get_insights() -> Dict[str, Any]:
    response = table.scan()
    items: List[Dict[str, Any]] = response.get("Items", [])

    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    total = len(items)
    sentiment_counts = {"positive": 0, "negative": 0, "neutral": 0}
    summaries: List[str] = []
    topics: List[str] = []

    for item in items:
        sentiment = (item.get("sentiment") or "").lower()
        if sentiment in sentiment_counts:
            sentiment_counts[sentiment] += 1

        if item.get("summary"):
            summaries.append(item["summary"])

        if isinstance(item.get("topics"), list):
            topics.extend(item["topics"])

    return {
        "statusCode": 200,
        "headers": _cors_headers(),
        "body": json.dumps({
            "totalSubmissions": total,
            "sentimentCounts": sentiment_counts,
            "summaries": summaries,
            "topics": topics,
        }),
    }


def call_ai_analysis(message: str) -> Dict[str, Any]:
    if AI_PROVIDER == "comprehend":
        return call_comprehend_analysis(message)
    return call_bedrock_analysis(message)


def call_comprehend_analysis(message: str) -> Dict[str, Any]:
    text = message.strip()[:5000]

    sentiment_resp = comprehend.detect_sentiment(Text=text, LanguageCode="en")
    sentiment_raw = sentiment_resp.get("Sentiment", "NEUTRAL")

    sentiment_map = {
        "POSITIVE": "positive",
        "NEGATIVE": "negative",
        "NEUTRAL": "neutral",
        "MIXED": "neutral",
    }

    sentiment = sentiment_map.get(sentiment_raw, "neutral")

    key_phrases_resp = comprehend.detect_key_phrases(Text=text, LanguageCode="en")
    topics = [
        p.get("Text")
        for p in key_phrases_resp.get("KeyPhrases", [])
        if p.get("Text")
    ]

    summary = "Key themes: " + ", ".join(topics[:5]) if topics else ""

    return {
        "sentiment": sentiment,
        "topics": topics,
        "summary": summary,
    }


def call_bedrock_analysis(message: str) -> Dict[str, Any]:
    prompt = f"""
Analyze the following employee feedback and return STRICT JSON with:
- sentiment (positive/negative/neutral)
- topics (array)
- summary (one sentence)

Feedback:
\"\"\"{message}\"\"\"
""".strip()

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 256,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }
        ],
    }

    response = bedrock.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )

    response_body = json.loads(response["body"].read())

    text_chunks = [
        c.get("text", "")
        for c in response_body.get("content", [])
        if c.get("type") == "text"
    ]

    combined = "".join(text_chunks).strip()

    parsed = json.loads(combined)

    return {
        "sentiment": parsed.get("sentiment", "neutral"),
        "topics": parsed.get("topics", []),
        "summary": parsed.get("summary", ""),
    }
