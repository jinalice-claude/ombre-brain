"""
========================================
src/public_api.py — 对外公开 HTTP API（自定义层，上游没有）
========================================

迁移自旧单体 server.py 的 /api/public/* 端点：小角落前端（jiner-keke）赖以
读写记忆、信箱、叽叽喳喳、TTS 的对外接口。全部走 X-Public-Token 鉴权。

设计：
- 不反向 import server.py；依赖（bucket_mgr/config/logger）从 tools._runtime 取，
  工具逻辑直接调 tools.breath/hold/trace 的 dispatch()。
- server.py 在创建好 mcp 实例并完成 rt.init() 后，调用 register(mcp) 注册全部路由。

对外暴露：register(mcp) / _require_public_token(request)
========================================
"""

import os
import hmac
import threading
import uuid
from datetime import datetime, timezone

import httpx

from tools import _runtime as rt
from tools import breath as _t_breath
from tools import hold as _t_hold
from tools import trace as _t_trace

# --- 鉴权共享密钥：来自环境变量，绝不硬编码 ---
PUBLIC_API_TOKEN = os.environ.get("OMBRE_PUBLIC_TOKEN", "").strip()

# --- ElevenLabs Cove voice ID（/api/public/tts 使用）---
COVE_VOICE_ID = "dANrAVjrqr8FrYBGY3x4"

# --- chirps.json 读-改-写串行锁 ---
_chirps_lock = threading.Lock()


def _require_public_token(request):
    """X-Public-Token 缺失/错误时返回错误响应，正确则返回 None。"""
    from starlette.responses import JSONResponse
    if not PUBLIC_API_TOKEN:
        return JSONResponse(
            {"error": "Public API is not configured on this server"},
            status_code=503, headers={"Access-Control-Allow-Origin": "*"},
        )
    token = request.headers.get("X-Public-Token", "")
    if not hmac.compare_digest(token, PUBLIC_API_TOKEN):
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401, headers={"Access-Control-Allow-Origin": "*"},
        )
    return None


def register(mcp):
    """server.py 启动时调用一次，把所有 /api/public/* 路由挂到 mcp 上。"""

    # ---------- 写入记忆 ----------
    @mcp.custom_route("/api/public/hold", methods=["POST", "OPTIONS"])
    async def api_public_hold(request):
        from starlette.responses import JSONResponse, Response
        if request.method == "OPTIONS":
            r = Response()
            r.headers["Access-Control-Allow-Origin"] = "*"
            r.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Public-Token"
            return r
        auth_err = _require_public_token(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
            content = body.get("content", "")
            tags = body.get("tags", "")
            importance = int(body.get("importance", 5))
            if not content:
                return JSONResponse({"error": "empty"}, status_code=400, headers={"Access-Control-Allow-Origin": "*"})
            result = await _t_hold.dispatch(content=content, tags=tags, importance=importance)
            return JSONResponse({"ok": True, "result": result}, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            rt.logger.error(f"api_public_hold error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500, headers={"Access-Control-Allow-Origin": "*"})

    # ---------- 检索 / 浮现 ----------
    @mcp.custom_route("/api/public/breath", methods=["POST", "OPTIONS"])
    async def api_public_breath(request):
        from starlette.responses import JSONResponse, Response
        if request.method == "OPTIONS":
            r = Response()
            r.headers["Access-Control-Allow-Origin"] = "*"
            r.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Public-Token"
            return r
        auth_err = _require_public_token(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
            query = body.get("query", "")
            max_tokens = int(body.get("max_tokens", 6000))
            result = await _t_breath.dispatch(query=query, max_tokens=max_tokens)
            return JSONResponse({"ok": True, "result": result}, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            rt.logger.error(f"api_public_breath error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500, headers={"Access-Control-Allow-Origin": "*"})

    # ---------- 列出全部桶（内容级）----------
    @mcp.custom_route("/api/public/list", methods=["GET", "OPTIONS"])
    async def api_public_list(request):
        from starlette.responses import JSONResponse, Response
        if request.method == "OPTIONS":
            r = Response()
            r.headers["Access-Control-Allow-Origin"] = "*"
            r.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            r.headers["Access-Control-Allow-Headers"] = "X-Public-Token"
            return r
        auth_err = _require_public_token(request)
        if auth_err:
            return auth_err
        try:
            tag = request.query_params.get("tag", "")
            all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
            result = []
            for b in all_buckets:
                tags = b.get("metadata", {}).get("tags", []) or []
                if tag and tag not in tags:
                    continue
                result.append({
                    "id": b["id"],
                    "content": b.get("content", ""),
                    "tags": tags,
                    "created": b.get("metadata", {}).get("created", ""),
                })
            result.sort(key=lambda x: x["created"], reverse=True)
            return JSONResponse(result, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500, headers={"Access-Control-Allow-Origin": "*"})

    # ---------- 只读全量导出（备份用，B8 删）----------
    @mcp.custom_route("/api/public/export", methods=["GET", "OPTIONS"])
    async def api_public_export(request):
        """只读全量导出：把 buckets_dir 整个打包成 tar.gz 返回。仅备份用。"""
        from starlette.responses import Response, JSONResponse
        if request.method == "OPTIONS":
            r = Response()
            r.headers["Access-Control-Allow-Origin"] = "*"
            r.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            r.headers["Access-Control-Allow-Headers"] = "X-Public-Token"
            return r
        auth_err = _require_public_token(request)
        if auth_err:
            return auth_err
        try:
            import io, tarfile
            base_dir = rt.bucket_mgr.base_dir
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                tar.add(base_dir, arcname="buckets")
            return Response(
                content=buf.getvalue(),
                media_type="application/gzip",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Content-Disposition": "attachment; filename=ombre-buckets.tar.gz",
                },
            )
        except Exception as e:
            rt.logger.error(f"api_public_export error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500, headers={"Access-Control-Allow-Origin": "*"})

    # ---------- 删除桶 ----------
    @mcp.custom_route("/api/public/delete", methods=["POST", "OPTIONS"])
    async def api_public_delete(request):
        from starlette.responses import JSONResponse, Response
        if request.method == "OPTIONS":
            r = Response()
            r.headers["Access-Control-Allow-Origin"] = "*"
            r.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Public-Token"
            return r
        auth_err = _require_public_token(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
            bucket_id = body.get("bucket_id", "")
            if not bucket_id:
                return JSONResponse({"error": "missing bucket_id"}, status_code=400, headers={"Access-Control-Allow-Origin": "*"})
            result = await _t_trace.dispatch(bucket_id=bucket_id, delete=True)
            if "not found" in str(result).lower():
                return JSONResponse({"error": "not found"}, status_code=404, headers={"Access-Control-Allow-Origin": "*"})
            return JSONResponse({"ok": True}, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500, headers={"Access-Control-Allow-Origin": "*"})

    # ---------- 更新桶内容 ----------
    @mcp.custom_route("/api/public/update", methods=["POST", "OPTIONS"])
    async def api_public_update(request):
        from starlette.responses import JSONResponse, Response
        if request.method == "OPTIONS":
            r = Response()
            r.headers["Access-Control-Allow-Origin"] = "*"
            r.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Public-Token"
            return r
        auth_err = _require_public_token(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
            bucket_id = body.get("bucket_id", "")
            content = body.get("content", "")
            if not bucket_id or not content:
                return JSONResponse({"error": "missing fields"}, status_code=400, headers={"Access-Control-Allow-Origin": "*"})
            await rt.bucket_mgr.update(bucket_id, content=content)
            return JSONResponse({"ok": True}, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500, headers={"Access-Control-Allow-Origin": "*"})

    # ---------- ElevenLabs TTS 代理 ----------
    @mcp.custom_route("/api/public/tts", methods=["POST", "OPTIONS"])
    async def api_public_tts(request):
        from starlette.responses import JSONResponse, Response
        if request.method == "OPTIONS":
            r = Response()
            r.headers["Access-Control-Allow-Origin"] = "*"
            r.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Public-Token"
            return r
        auth_err = _require_public_token(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
            text = body.get("text", "")
            if not text:
                return JSONResponse({"error": "text is required"}, status_code=400, headers={"Access-Control-Allow-Origin": "*"})
            if len(text) > 5000:
                return JSONResponse({"error": "text too long"}, status_code=400, headers={"Access-Control-Allow-Origin": "*"})
            api_key = os.environ["ELEVENLABS_API_KEY"]
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{COVE_VOICE_ID}"
            payload = {"text": text, "model_id": "eleven_multilingual_v2"}
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers={"xi-api-key": api_key})
            if resp.status_code == 200:
                return Response(content=resp.content, media_type="audio/mpeg",
                                headers={"Access-Control-Allow-Origin": "*"})
            return JSONResponse(
                {"error": "ElevenLabs error", "status": resp.status_code, "detail": resp.text},
                status_code=resp.status_code, headers={"Access-Control-Allow-Origin": "*"},
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500, headers={"Access-Control-Allow-Origin": "*"})

    # ---------- Plan 信箱（POST 带 token 投递 / GET 无鉴权纯文本读取）----------
    @mcp.custom_route("/api/public/plan", methods=["GET", "POST", "OPTIONS"])
    @mcp.custom_route("/api/public/mailbox", methods=["GET", "OPTIONS"], name="api_public_mailbox")
    async def api_public_plan(request):
        """计划信箱：POST（带 token）投递最新 plan，GET（无鉴权纯文本）供手机端读取。"""
        from starlette.responses import JSONResponse, PlainTextResponse, Response
        # 放在 mailbox/ 子目录下，桶管理器只扫固定子目录，不会误读
        plan_path = os.path.join(rt.bucket_mgr.base_dir, "mailbox", "latest_plan.txt")

        if request.method == "OPTIONS":
            r = Response()
            r.headers["Access-Control-Allow-Origin"] = "*"
            r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Public-Token"
            return r

        if request.method == "GET":
            # 无鉴权：手机端 web_fetch 带不了 header；内容只有最新一份计划，不含密钥
            try:
                with open(plan_path, "r", encoding="utf-8") as f:
                    text = f.read()
            except FileNotFoundError:
                text = "信箱是空的"
            return PlainTextResponse(text, headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache, no-store, must-revalidate",
            })

        auth_err = _require_public_token(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
            content = body.get("content", "")
            if not content:
                return JSONResponse({"error": "content is required"}, status_code=400, headers={"Access-Control-Allow-Origin": "*"})
            if len(content) > 50000:
                return JSONResponse({"error": "content too long (max 50000 chars)"}, status_code=400, headers={"Access-Control-Allow-Origin": "*"})
            os.makedirs(os.path.dirname(plan_path), exist_ok=True)
            with open(plan_path, "w", encoding="utf-8") as f:
                f.write(content)
            return JSONResponse({"ok": True, "length": len(content)}, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            rt.logger.error(f"api_public_plan error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500, headers={"Access-Control-Allow-Origin": "*"})

    # ---------- 叽叽喳喳 ----------
    @mcp.custom_route("/api/public/chirps", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    async def api_public_chirps(request):
        """GET 倒序返回全部；POST 追加(≤140字)；PUT 按 id 改文字(ts 不动)；DELETE 按 id 删。"""
        from starlette.responses import JSONResponse, Response
        import json as _json_lib
        chirps_path = os.path.join(rt.bucket_mgr.base_dir, "chirps.json")
        cors = {"Access-Control-Allow-Origin": "*"}

        if request.method == "OPTIONS":
            r = Response()
            r.headers["Access-Control-Allow-Origin"] = "*"
            r.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Public-Token"
            return r

        auth_err = _require_public_token(request)
        if auth_err:
            return auth_err

        def _read_chirps():
            try:
                with open(chirps_path, "r", encoding="utf-8") as f:
                    return _json_lib.load(f)
            except FileNotFoundError:
                return []

        def _write_chirps(data):
            # 先写临时文件再 os.replace 原子替换，写到一半崩溃也不损坏正式文件
            tmp_path = chirps_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                _json_lib.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, chirps_path)

        def _validate_text(body):
            text = str(body.get("text", "")).strip()
            if not text:
                return None, JSONResponse({"error": "text is required"}, status_code=400, headers=cors)
            if len(text) > 140:
                return None, JSONResponse({"error": "text too long (max 140 chars)"}, status_code=400, headers=cors)
            return text, None

        try:
            if request.method == "GET":
                with _chirps_lock:
                    chirps = _read_chirps()
                chirps.sort(key=lambda c: c.get("ts", ""), reverse=True)
                return JSONResponse(chirps, headers=cors)

            body = await request.json()

            if request.method == "POST":
                text, err = _validate_text(body)
                if err:
                    return err
                entry = {"id": uuid.uuid4().hex[:12], "text": text,
                         "ts": datetime.now(timezone.utc).isoformat()}
                with _chirps_lock:
                    chirps = _read_chirps()
                    chirps.append(entry)
                    _write_chirps(chirps)
                return JSONResponse(entry, headers=cors)

            chirp_id = str(body.get("id", "")).strip()
            if not chirp_id:
                return JSONResponse({"error": "id is required"}, status_code=400, headers=cors)

            if request.method == "PUT":
                text, err = _validate_text(body)
                if err:
                    return err
                with _chirps_lock:
                    chirps = _read_chirps()
                    target = next((c for c in chirps if c.get("id") == chirp_id), None)
                    if target is None:
                        return JSONResponse({"error": "chirp not found"}, status_code=404, headers=cors)
                    target["text"] = text   # ts 不动：改字不改它落下的那一天
                    _write_chirps(chirps)
                return JSONResponse(target, headers=cors)

            # DELETE
            with _chirps_lock:
                chirps = _read_chirps()
                remaining = [c for c in chirps if c.get("id") != chirp_id]
                if len(remaining) == len(chirps):
                    return JSONResponse({"error": "chirp not found"}, status_code=404, headers=cors)
                _write_chirps(remaining)
            return JSONResponse({"ok": True}, headers=cors)
        except Exception as e:
            rt.logger.error(f"api_public_chirps error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500, headers=cors)
