"""Dependency-free HTTP server for the local video-document workflow."""

from __future__ import annotations

import argparse
from ipaddress import ip_address
import json
import logging
import re
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlsplit

from video_doc.web_service import TaskService, WebServiceError


LOGGER = logging.getLogger("video_doc.web")
MAX_JSON_BYTES = 4 * 1024 * 1024
TASK_ROUTE = r"(?P<task_id>[A-Za-z0-9][A-Za-z0-9_-]{0,63})"
VERSION_ROUTE = r"(?P<version_id>machine|latest|editorial-[0-9a-f]{12})"
STATIC_ROOT = Path(__file__).resolve().parent / "webui"


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def _file_response(
    handler: BaseHTTPRequestHandler,
    path: Path,
    *,
    content_type: str,
    download_name: str | None = None,
    cache_control: str = "no-store",
) -> None:
    try:
        size = path.stat().st_size
        body = path.read_bytes()
    except OSError as exc:
        raise WebServiceError("文件无法读取。", status=404, code="FILE_NOT_FOUND") from exc
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(size))
    handler.send_header("Cache-Control", cache_control)
    handler.send_header("X-Content-Type-Options", "nosniff")
    if download_name is not None:
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", download_name)
        handler.send_header(
            "Content-Disposition",
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(download_name)}",
        )
    handler.end_headers()
    handler.wfile.write(body)


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class VideoDocRequestHandler(BaseHTTPRequestHandler):
    server: "VideoDocHTTPServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _validate_request_source(self) -> None:
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if port == 80:
            allowed_hosts.update({"127.0.0.1", "localhost", "[::1]"})
        origins = self.headers.get_all("Origin", [])
        if (
            not ip_address(self.client_address[0]).is_loopback
            or len(hosts) != 1
            or hosts[0].lower() not in allowed_hosts
            or (origins and origins != [f"http://{hosts[0].lower()}"])
            or self.headers.get("Sec-Fetch-Site") == "cross-site"
        ):
            raise WebServiceError("请求必须来自本机工作区。", status=403, code="INVALID_ORIGIN")

    def _request_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise WebServiceError("请求长度无效。", code="INVALID_REQUEST") from exc
        if length <= 0:
            return {}
        if length > MAX_JSON_BYTES:
            raise WebServiceError("请求体过大。", status=413, code="REQUEST_TOO_LARGE")
        if self.headers.get_content_type() != "application/json":
            raise WebServiceError("请求须使用JSON。", status=415, code="INVALID_CONTENT_TYPE")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise WebServiceError("请求体不是有效JSON。", code="INVALID_JSON") from exc
        if not isinstance(value, dict):
            raise WebServiceError("请求体必须是JSON对象。", code="INVALID_JSON")
        return value

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query, keep_blank_values=True)

    @staticmethod
    def _query_one(query: Mapping[str, list[str]], key: str, default: str = "") -> str:
        values = query.get(key)
        return values[0] if values else default

    def _dispatch_error(self, exc: Exception) -> None:
        # Error paths may leave a rejected upload body unread; do not reuse that connection.
        self.close_connection = True
        if isinstance(exc, WebServiceError):
            _json_response(
                self,
                exc.status,
                {"error": {"code": exc.code, "message": str(exc)}},
            )
            return
        LOGGER.exception("Unhandled web request error")
        _json_response(
            self,
            500,
            {"error": {"code": "INTERNAL_ERROR", "message": "服务器处理请求时发生错误。"}},
        )

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._validate_request_source()
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            service = self.server.service

            if path == "/api/health":
                _json_response(self, 200, {"status": "ok", "system": service.environment_status()})
                return
            if path == "/api/overview":
                _json_response(self, 200, service.overview())
                return
            if path == "/api/tasks":
                query = self._query()
                tasks = service.list_tasks(
                    status=self._query_one(query, "status", "all"),
                    search=self._query_one(query, "search"),
                    include_archived=_parse_bool(self._query_one(query, "archived"), False),
                )
                _json_response(self, 200, {"tasks": tasks, "count": len(tasks)})
                return

            match = re.fullmatch(f"/api/tasks/{TASK_ROUTE}/document", path)
            if match:
                query = self._query()
                version = self._query_one(query, "version") or None
                _json_response(
                    self,
                    200,
                    service.get_document(match.group("task_id"), version),
                )
                return

            match = re.fullmatch(f"/api/tasks/{TASK_ROUTE}/edits", path)
            if match:
                task = service.get_task(match.group("task_id"))
                _json_response(self, 200, {"versions": task["edits"]})
                return

            match = re.fullmatch(f"/api/tasks/{TASK_ROUTE}/frames/(?P<frame_id>[A-Za-z0-9_-]{{1,80}})", path)
            if match:
                image_path, mime = service.frame_file(
                    match.group("task_id"), match.group("frame_id"), self._query_one(self._query(), "version", "machine")
                )
                _file_response(
                    self,
                    image_path,
                    content_type=mime,
                    cache_control="private, max-age=3600",
                )
                return

            match = re.fullmatch(
                f"/api/tasks/{TASK_ROUTE}/download/{VERSION_ROUTE}/(?P<format>json|md|docx)",
                path,
            )
            if match:
                file_path, download_name, mime = service.export_file(
                    match.group("task_id"),
                    match.group("version_id"),
                    match.group("format"),
                )
                _file_response(
                    self,
                    file_path,
                    content_type=mime,
                    download_name=download_name,
                )
                return

            match = re.fullmatch(f"/api/tasks/{TASK_ROUTE}", path)
            if match:
                _json_response(self, 200, service.get_task(match.group("task_id")))
                return

            self._serve_static(path)
        except Exception as exc:
            self._dispatch_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._validate_request_source()
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            query = self._query()
            service = self.server.service

            if path == "/api/tasks":
                task = service.create_local_task(self._request_json())
                _json_response(self, HTTPStatus.CREATED, task)
                return

            if path == "/api/tasks/upload":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise WebServiceError("上传长度无效。", code="INVALID_UPLOAD") from exc
                task = service.create_uploaded_task(
                    task_id=self._query_one(query, "task_id") or None,
                    filename=self._query_one(query, "filename", "source-video.mp4"),
                    content_length=length,
                    stream=self.rfile,
                    display_name=self._query_one(query, "display_name") or None,
                    options={
                        "ocr_mode": self._query_one(query, "ocr_mode", "auto"),
                        "bounded_content": _parse_bool(
                            self._query_one(query, "bounded_content"), True
                        ),
                        "allow_llm": _parse_bool(
                            self._query_one(query, "allow_llm"), False
                        ),
                        "ffmpeg_dir": self._query_one(query, "ffmpeg_dir"),
                    },
                )
                _json_response(self, HTTPStatus.CREATED, task)
                return

            match = re.fullmatch(f"/api/tasks/{TASK_ROUTE}/run", path)
            if match:
                task = service.start_task(match.group("task_id"), self._request_json())
                _json_response(self, HTTPStatus.ACCEPTED, task)
                return

            match = re.fullmatch(f"/api/tasks/{TASK_ROUTE}/cancel", path)
            if match:
                _json_response(self, 202, service.cancel_task(match.group("task_id")))
                return

            match = re.fullmatch(f"/api/tasks/{TASK_ROUTE}/archive", path)
            if match:
                _json_response(self, 200, service.set_archived(match.group("task_id"), True))
                return

            match = re.fullmatch(f"/api/tasks/{TASK_ROUTE}/restore", path)
            if match:
                _json_response(self, 200, service.set_archived(match.group("task_id"), False))
                return

            match = re.fullmatch(f"/api/tasks/{TASK_ROUTE}/edits", path)
            if match:
                payload = self._request_json()
                _json_response(
                    self,
                    HTTPStatus.CREATED,
                    service.save_edit(match.group("task_id"), payload),
                )
                return

            match = re.fullmatch(f"/api/tasks/{TASK_ROUTE}/exports", path)
            if match:
                payload = self._request_json()
                version_id = payload.get("version_id")
                if not isinstance(version_id, str) or not version_id:
                    raise WebServiceError("请选择要导出的版本。", code="VERSION_REQUIRED")
                _json_response(
                    self,
                    200,
                    service.prepare_export(match.group("task_id"), version_id),
                )
                return

            raise WebServiceError("接口不存在。", status=404, code="NOT_FOUND")
        except Exception as exc:
            self._dispatch_error(exc)

    def _serve_static(self, path: str) -> None:
        if path.startswith("/assets/"):
            relative = path.removeprefix("/assets/")
        else:
            relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in candidate.parents and candidate != STATIC_ROOT.resolve():
            raise WebServiceError("页面不存在。", status=404, code="NOT_FOUND")
        if not candidate.is_file():
            if not Path(path).suffix:
                candidate = STATIC_ROOT / "index.html"
            else:
                raise WebServiceError("页面资源不存在。", status=404, code="NOT_FOUND")
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(candidate.suffix.lower(), "application/octet-stream")
        _file_response(
            self,
            candidate,
            content_type=mime,
            cache_control="no-cache",
        )


class VideoDocHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        service: TaskService,
    ) -> None:
        self.service = service
        super().__init__(server_address, VideoDocRequestHandler)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    output_root: str | Path = "outputs",
    service: TaskService | None = None,
) -> VideoDocHTTPServer:
    return VideoDocHTTPServer(
        (host, port),
        service or TaskService(output_root=output_root),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动知识视频文档生成系统本地界面")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--output-root", default="outputs", help="任务输出根目录")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--verbose", action="store_true", help="输出详细请求日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    service = TaskService(output_root=args.output_root)
    try:
        server = VideoDocHTTPServer((args.host, args.port), service)
    except OSError as exc:
        print(f"无法启动服务：{exc}", file=sys.stderr)
        return 1
    host, port = server.server_address[:2]
    url_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{url_host}:{port}/"
    print("知识型视频文档系统已启动")
    print(f"访问地址：{url}")
    print("按 Ctrl+C 停止服务")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
