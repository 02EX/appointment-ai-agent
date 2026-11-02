"""
FastAPI AI 微服务入口。
对 Java 开发者的对应关系：
- FastAPI ≈ Spring Boot + Controller
- GET 查询参数 ≈ @RequestParam
- 启动命令：uvicorn main:app --reload --port 8000
"""

from datetime import datetime, timedelta
import json
import re

import redis
import requests
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

# ============ 请把这里改成你自己的 DeepSeek API Key ============
DEEPSEEK_API_KEY = "sk-your-key-here"
# ============================================================

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
JAVA_BOOK_URL = "http://localhost:8080/appointment/admin/ai-book"
JAVA_SLOTS_URL = "http://localhost:8080/appointment/available-slots"
DEFAULT_SERVICE_ITEM_ID = "2"
BUSINESS_HOURS_RULE = (
    "本店营业时间为每天 09:00-12:00 和 14:00-18:00。"
    "如果用户要求的时间不在此时段内，请礼貌拒绝，并引导用户重新选择时间。"
)
MOCK_LEGAL_SLOTS = [
    "09:00-10:00",
    "10:00-11:00",
    "11:00-12:00",
    "14:00-15:00",
    "15:00-16:00",
    "16:00-17:00",
    "17:00-18:00",
]

REDIS_HOST = "localhost"
REDIS_PORT = 6379
CHAT_HISTORY_TTL_SECONDS = 60 * 60 * 24  # 会话历史保留 24 小时
MAX_HISTORY_MESSAGES = 20

MOCK_PROJECT_MENU = """
【可预约项目菜单】
当前美容系统只有 1 个可预约项目：
- 项目ID: 2，名称: 肩颈舒缓按摩，时长: 45分钟
用户提到洗牙、按摩、护理等预约时，一律映射到该项目（serviceItemId=2）。
"""

app = FastAPI(title="Python AI Service", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_memory_history = {}


def get_redis_client():
    """连接 Redis；连不上时返回 None，改用进程内内存（方便本机先跑通）。"""
    try:
        client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True,
            socket_connect_timeout=1,
        )
        client.ping()
        return client
    except Exception:
        return None


def history_key(session_id: str) -> str:
    return f"ai:chat:{session_id}"


def load_history(session_id: str) -> list:
    client = get_redis_client()
    if client is not None:
        raw = client.get(history_key(session_id))
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    return list(_memory_history.get(session_id, []))


def save_history(session_id: str, messages: list) -> None:
    trimmed = messages[-MAX_HISTORY_MESSAGES:]
    client = get_redis_client()
    if client is not None:
        client.set(
            history_key(session_id),
            json.dumps(trimmed, ensure_ascii=False),
            ex=CHAT_HISTORY_TTL_SECONDS,
        )
        return
    _memory_history[session_id] = trimmed


def deepseek_headers() -> dict:
    return {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }


def _parse_hhmm(time_text: str):
    raw = (time_text or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def to_time_slot(time_text: str) -> str:
    """把 15:00 这类开始时间拼成 Java 需要的 timeSlot：15:00-16:00。"""
    raw = (time_text or "").strip()
    if not raw:
        return raw
    if "-" in raw:
        start_text, end_text = [p.strip() for p in raw.split("-", 1)]
        start = _parse_hhmm(start_text)
        end = _parse_hhmm(end_text)
        if start and end:
            return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
        return raw
    start = _parse_hhmm(raw)
    if not start:
        return raw
    end = start + timedelta(hours=1)
    return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"


def mock_slots_for_date(date_text: str) -> list:
    """营业时间内的合法 Mock 时段；当天会过滤已经过去的格子。"""
    slots = list(MOCK_LEGAL_SLOTS)
    today = datetime.now()
    if date_text == today.strftime("%Y-%m-%d"):
        now_hm = today.strftime("%H:%M")
        slots = [s for s in slots if s.split("-")[0] > now_hm]
    return slots


def _collect_slot_strings(value) -> list:
    found = []
    if value is None:
        return found
    if isinstance(value, str):
        text = value.strip()
        if text:
            found.append(to_time_slot(text) if re.match(r"^\d{1,2}:\d{2}", text) else text)
        return found
    if isinstance(value, list):
        for item in value:
            found.extend(_collect_slot_strings(item))
        return found
    if isinstance(value, dict):
        for key in ("timeSlot", "time_slot", "slot", "time", "startTime", "start"):
            if key in value and value[key] not in (None, ""):
                found.extend(_collect_slot_strings(value[key]))
                return found
        for key in ("data", "slots", "availableSlots", "records", "list", "content"):
            if key in value:
                found.extend(_collect_slot_strings(value[key]))
                if found:
                    return found
        for item in value.values():
            found.extend(_collect_slot_strings(item))
    return found


def parse_slots_payload(payload) -> list:
    slots = []
    seen = set()
    for raw in _collect_slot_strings(payload):
        slot = to_time_slot(str(raw))
        if slot and slot not in seen and re.match(r"^\d{2}:\d{2}-\d{2}:\d{2}$", slot):
            seen.add(slot)
            slots.append(slot)
    return slots


def fetch_available_slots(date_text: str, service_item_id: str) -> dict:
    """问 Java 某天还有哪些空闲时段；调不通则用营业时间内的 Mock 时段。"""
    try:
        resp = requests.get(
            JAVA_SLOTS_URL,
            params={"date": date_text, "serviceItemId": service_item_id},
            timeout=5,
        )
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        return {
            "source": "java",
            "date": date_text,
            "slots": parse_slots_payload(body),
        }
    except Exception:
        pass
    return {
        "source": "mock",
        "date": date_text,
        "slots": mock_slots_for_date(date_text),
    }


def extra_dates_from_text(text: str) -> list:
    return re.findall(r"\d{4}-\d{2}-\d{2}", text or "")


def fetch_slots_window(service_item_id: str, extra_dates=None, days: int = 7) -> list:
    """调用大模型前拉取未来几天（及用户提到的日期）的可用时段。"""
    today = datetime.now().date()
    dates = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    for extra in extra_dates or []:
        if extra not in dates:
            dates.append(extra)

    results = []
    use_java = True
    for date_text in dates:
        if use_java:
            item = fetch_available_slots(date_text, service_item_id)
            if item["source"] != "java":
                use_java = False
            results.append(item)
        else:
            results.append(
                {
                    "source": "mock",
                    "date": date_text,
                    "slots": mock_slots_for_date(date_text),
                }
            )
    return results


def format_slots_for_prompt(window: list) -> str:
    lines = []
    for item in window:
        slots = "、".join(item["slots"]) if item["slots"] else "无可预约时段"
        lines.append(f"- {item['date']}：{slots}")
    return "\n".join(lines)


def slot_is_available(date_text: str, time_text: str, window: list) -> bool:
    wanted = to_time_slot(time_text)
    for item in window:
        if item["date"] == date_text:
            allowed = {to_time_slot(s) for s in item["slots"]}
            return wanted in allowed
    return False


def call_java_book(user_id: str, date: str, time: str, service_item_id: str) -> dict:
    """调用 Java /admin/ai-book（@RequestParam 表单参数）；调不通则 Mock。"""
    payload = {
        "userId": user_id,
        "serviceItemId": service_item_id,
        "date": date,
        "timeSlot": to_time_slot(time),
    }
    try:
        # Java 用 @RequestParam 接收，必须发 application/x-www-form-urlencoded
        resp = requests.post(JAVA_BOOK_URL, data=payload, timeout=5)
        resp.raise_for_status()
        try:
            return {
                "ok": True,
                "source": "java",
                "data": resp.json(),
            }
        except ValueError:
            return {
                "ok": True,
                "source": "java",
                "data": resp.text,
            }
    except Exception as exc:
        return {
            "ok": True,
            "source": "mock",
            "message": "Java 接口不可用，已 Mock 预约成功",
            "request": payload,
            "error": str(exc),
        }


BOOK_TOOL = {
    "type": "function",
    "function": {
        "name": "book_appointment",
        "description": "当用户明确要预约，并且已经提供日期、时间和项目时，调用此函数完成预约落库。",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "预约日期，格式 YYYY-MM-DD",
                },
                "time": {
                    "type": "string",
                    "description": "预约开始时间 HH:mm，例如 15:00；Python 会自动拼成 15:00-16:00。必须来自可用时段列表。",
                },
                "serviceItemId": {
                    "type": "string",
                    "description": "服务项目ID，当前系统只有肩颈舒缓按摩，固定为 2",
                },
            },
            "required": ["date", "time", "serviceItemId"],
        },
    },
}


@app.get("/")
def health():
    return {"status": "ok", "service": "python-ai-service"}


@app.get("/api/ai/chat-stream")
def chat_stream(
    sessionId: str = Query(..., description="会话 ID，用来读写 Redis 历史"),
    message: str = Query(..., description="用户当前输入"),
):
    """流式对话：读 Redis 历史 -> 调 DeepSeek stream -> SSE 打字机返回。"""

    history = load_history(sessionId)
    history.append({"role": "user", "content": message})

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": history,
        "stream": True,
    }

    def event_generator():
        assistant_text = []
        try:
            with requests.post(
                DEEPSEEK_URL,
                headers=deepseek_headers(),
                json=payload,
                stream=True,
                timeout=120,
            ) as resp:
                if resp.status_code != 200:
                    yield {
                        "event": "error",
                        "data": json.dumps(
                            {
                                "status": resp.status_code,
                                "body": resp.text,
                                "hint": "请检查 DEEPSEEK_API_KEY 是否已替换为真实 Key",
                            },
                            ensure_ascii=False,
                        ),
                    }
                    return

                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = (
                        chunk.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content")
                    )
                    if not delta:
                        continue
                    assistant_text.append(delta)
                    # 每个 token 推一条 SSE，前端按 data 拼接即可做出打字机效果
                    yield {"event": "message", "data": delta}

            full_reply = "".join(assistant_text)
            if full_reply:
                history.append({"role": "assistant", "content": full_reply})
                save_history(sessionId, history)

            yield {
                "event": "done",
                "data": json.dumps({"sessionId": sessionId}, ensure_ascii=False),
            }
        except Exception as exc:
            yield {
                "event": "error",
                "data": json.dumps({"error": str(exc)}, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())


def persist_turn(session_id: str, history: list, user_message: str, assistant_reply: str) -> None:
    """把本轮 user / assistant 追加进会话历史（不保存 system，方便每轮刷新当前时间）。"""
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": assistant_reply})
    save_history(session_id, history)


@app.get("/api/ai/agent")
def agent(
    userId: str = Query(..., description="当前用户 ID"),
    message: str = Query(..., description="用户当前输入"),
    sessionId: str = Query("default", description="会话 ID，相同 ID 会延续多轮对话"),
):
    """
    Agent：系统提示词注入当前时间和 Mock 项目菜单，
    走 DeepSeek Function Calling；提取到预约参数后调用 Java 落库。
    通过 sessionId 读取/写入对话历史，支持多轮上下文。
    """
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    slots_window = fetch_slots_window(
        DEFAULT_SERVICE_ITEM_ID,
        extra_dates=extra_dates_from_text(message),
    )
    available_slots_text = format_slots_for_prompt(slots_window)
    system_prompt = f"""你是预约助手。当前服务器时间是：{now_text}。
{MOCK_PROJECT_MENU}

营业规则：
{BUSINESS_HOURS_RULE}

【真实可用时段】（只能从下面列表里选，不要编造；午休 12:00-14:00 不可约）
{available_slots_text}

规则：
1. 根据用户意图帮助预约。日期/时间可以相对当前时间理解（例如“明天下午三点”→ 15:00）。
2. 当前系统只有「肩颈舒缓按摩」，serviceItemId 固定为 {DEFAULT_SERVICE_ITEM_ID}。
3. time 参数填开始时间 HH:mm（例如 15:00）。系统会在调用 Java 前自动拼成 15:00-16:00。
4. 用户要的时间不在营业时间或不在可用时段列表中时，礼貌拒绝并列出可约时间，不要调用函数。
5. 只有当日期、时间和项目都齐全，且时间属于可用时段时，才调用 book_appointment。
6. 信息不全时，用中文向用户追问，不要调用函数。
7. 预约成功后，用中文简短确认。
8. 必须结合历史对话理解指代（例如用户先问有什么项目，再说“那就约那个”）。
"""

    history = load_history(sessionId)
    messages = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": message},
    ]

    first_payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "tools": [BOOK_TOOL],
        "tool_choice": "auto",
        "stream": False,
    }

    try:
        first_resp = requests.post(
            DEEPSEEK_URL,
            headers=deepseek_headers(),
            json=first_payload,
            timeout=60,
        )
    except Exception as exc:
        return {"ok": False, "sessionId": sessionId, "error": f"调用 DeepSeek 失败: {exc}"}

    if first_resp.status_code != 200:
        return {
            "ok": False,
            "sessionId": sessionId,
            "status": first_resp.status_code,
            "body": first_resp.text,
            "hint": "请检查 DEEPSEEK_API_KEY 是否已替换为真实 Key",
        }

    first_json = first_resp.json()
    assistant_message = first_json["choices"][0]["message"]
    tool_calls = assistant_message.get("tool_calls") or []

    book_result = None
    if tool_calls:
        messages.append(assistant_message)
        for tool_call in tool_calls:
            fn = tool_call.get("function") or {}
            if fn.get("name") != "book_appointment":
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            date = str(args.get("date") or "")
            time = str(args.get("time") or args.get("timeSlot") or "")
            time_slot = to_time_slot(time)
            service_item_id = str(
                args.get("serviceItemId")
                or args.get("service_item_id")
                or args.get("projectId")
                or args.get("project_id")
                or DEFAULT_SERVICE_ITEM_ID
            )
            if not slot_is_available(date, time_slot, slots_window):
                book_result = {
                    "ok": False,
                    "source": "guard",
                    "message": "该时段不在营业时间或已不可预约，请改约可用时段",
                    "date": date,
                    "timeSlot": time_slot,
                    "available": available_slots_text,
                }
            else:
                book_result = call_java_book(userId, date, time_slot, service_item_id)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.get("id"),
                    "content": json.dumps(book_result, ensure_ascii=False),
                }
            )

        second_payload = {
            "model": DEEPSEEK_MODEL,
            "messages": messages,
            "stream": False,
        }
        second_resp = requests.post(
            DEEPSEEK_URL,
            headers=deepseek_headers(),
            json=second_payload,
            timeout=60,
        )
        if second_resp.status_code != 200:
            reply = "预约请求已提交，但生成确认文案失败。"
            persist_turn(sessionId, history, message, reply)
            return {
                "ok": True,
                "sessionId": sessionId,
                "booked": book_result,
                "reply": reply,
                "deepseekError": second_resp.text,
            }
        reply = second_resp.json()["choices"][0]["message"].get("content") or ""
        persist_turn(sessionId, history, message, reply)
        return {
            "ok": True,
            "sessionId": sessionId,
            "functionCalled": True,
            "booked": book_result,
            "reply": reply,
        }

    reply = assistant_message.get("content") or ""
    persist_turn(sessionId, history, message, reply)
    return {
        "ok": True,
        "sessionId": sessionId,
        "functionCalled": False,
        "booked": None,
        "reply": reply,
    }
