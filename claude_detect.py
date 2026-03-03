"""Claude Code 状态检测、窗口扫描、终端文本读取。"""
import os
import json
import glob
import logging
import time

import win32gui
from pywinauto import Desktop

from config import SPINNER_CHARS, state
from win32_api import get_window_title

logger = logging.getLogger("bedcode")

_windows_cache = []
_windows_cache_time = 0


def detect_claude_state(title: str) -> str:
    if not title:
        return "unknown"
    first_char = title[0] if title else ""
    if first_char in SPINNER_CHARS:
        return "thinking"
    # 各种可能的空闲前缀字符
    idle_chars = {"✳", "·", "•", "∙"}
    if first_char in idle_chars or "Claude" in title:
        return "idle"
    return "unknown"


def read_terminal_text(handle: int) -> str:
    try:
        from pywinauto import Application as PwaApp
        app = PwaApp(backend="uia").connect(handle=handle)
        win = app.window(handle=handle)
        for child in win.descendants():
            try:
                iface = child.iface_text
                if iface:
                    text = iface.DocumentRange.GetText(-1)
                    if isinstance(text, bytes):
                        text = text.decode('utf-8', errors='replace')
                    if text and len(text.strip()) > 10:
                        text = text.encode('utf-8', errors='replace').decode('utf-8')
                        return text
            except Exception:
                pass
            try:
                val = child.legacy_properties().get("Value", "")
                if isinstance(val, bytes):
                    val = val.decode('utf-8', errors='replace')
                if val and len(val.strip()) > 10:
                    val = val.encode('utf-8', errors='replace').decode('utf-8')
                    return val
            except Exception:
                pass
        return ""
    except Exception as e:
        logger.debug(f"UIA 文本读取失败: {e}")
        return ""


def read_last_transcript_response() -> str:
    claude_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    all_jsonl = glob.glob(os.path.join(claude_dir, "**", "*.jsonl"), recursive=True)
    all_jsonl = [f for f in all_jsonl if "subagent" not in f]
    if not all_jsonl:
        return ""
    latest = max(all_jsonl, key=os.path.getmtime)
    try:
        with open(latest, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return ""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        m = d.get("message", {})
        if m.get("role") != "assistant":
            continue
        content = m.get("content", [])
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            if parts:
                return "\n".join(parts)
    return ""


def _decode_proj_dirname(d: str) -> str:
    """Decode Claude project dir name like 'j-bedcode' -> 'j:\\bedcode'."""
    parts = d.split("-")
    if len(parts) >= 2 and len(parts[0]) == 1:
        return parts[0] + ":\\" + "\\".join(parts[1:])
    return d


def _get_active_projects_detail(max_count: int = 8) -> list[dict]:
    """Return [{name, dir_name, path}, ...] for recent projects."""
    projects_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    if not os.path.isdir(projects_dir):
        return []
    all_jsonl = glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))
    if not all_jsonl:
        return []
    all_jsonl.sort(key=os.path.getmtime, reverse=True)
    seen, result = [], []
    for f in all_jsonl:
        proj_dir = os.path.basename(os.path.dirname(f))
        if proj_dir not in seen:
            seen.append(proj_dir)
            parts = proj_dir.split("-")
            label = parts[-1] if parts else proj_dir
            result.append({"name": label, "dir_name": proj_dir, "path": _decode_proj_dirname(proj_dir)})
            if len(result) >= max_count:
                break
    return result


def _get_active_projects(max_count: int = 10) -> list[str]:
    projects_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    if not os.path.isdir(projects_dir):
        return []
    all_jsonl = glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))
    if not all_jsonl:
        return []
    all_jsonl.sort(key=os.path.getmtime, reverse=True)
    seen = []
    for f in all_jsonl:
        proj_dir = os.path.basename(os.path.dirname(f))
        if proj_dir not in seen:
            seen.append(proj_dir)
            if len(seen) >= max_count:
                break
    result = []
    for d in seen:
        parts = d.split("-")
        if len(parts) >= 2 and len(parts[0]) == 1:
            label = parts[-1] if parts[-1] else d
        else:
            label = parts[-1] if parts else d
        result.append(label)
    return result


# ── 费用计算 ─────────────────────────────────────────────────────
# Anthropic pricing per million tokens (Claude Opus 4 / Sonnet 4)
_PRICING = {
    "claude-opus-4": {"input": 15, "output": 75, "cache_read": 1.5, "cache_create": 18.75},
    "claude-sonnet-4": {"input": 3, "output": 15, "cache_read": 0.3, "cache_create": 3.75},
}


def _get_pricing(model: str) -> dict:
    for prefix, p in _PRICING.items():
        if model.startswith(prefix):
            return p
    return _PRICING["claude-opus-4"]  # fallback


def calc_session_cost() -> dict:
    """Parse the most recent JSONL transcript and sum token costs.
    Returns {"input_tokens":int, "output_tokens":int, "cache_read":int, "cache_create":int, "cost":float, "model":str, "turns":int}.
    """
    claude_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    all_jsonl = glob.glob(os.path.join(claude_dir, "**", "*.jsonl"), recursive=True)
    all_jsonl = [f for f in all_jsonl if "subagent" not in f]
    if not all_jsonl:
        return {"cost": 0.0, "turns": 0}
    latest = max(all_jsonl, key=os.path.getmtime)
    total_in = total_out = total_cache_read = total_cache_create = 0
    turns = 0
    model_name = ""
    try:
        with open(latest, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                m = d.get("message", {})
                if not isinstance(m, dict):
                    continue
                usage = m.get("usage")
                if not usage:
                    continue
                if not model_name:
                    model_name = m.get("model", "")
                total_in += usage.get("input_tokens", 0)
                total_out += usage.get("output_tokens", 0)
                total_cache_read += usage.get("cache_read_input_tokens", 0)
                total_cache_create += usage.get("cache_creation_input_tokens", 0)
                turns += 1
    except Exception:
        return {"cost": 0.0, "turns": 0}
    p = _get_pricing(model_name)
    cost = (
        total_in * p["input"] / 1_000_000
        + total_out * p["output"] / 1_000_000
        + total_cache_read * p["cache_read"] / 1_000_000
        + total_cache_create * p["cache_create"] / 1_000_000
    )
    return {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read": total_cache_read,
        "cache_create": total_cache_create,
        "cost": round(cost, 4),
        "model": model_name,
        "turns": turns,
    }


def _is_claude_window(title: str, class_name: str) -> bool:
    """判断窗口是否为 Claude Code 窗口（仅终端窗口）。"""
    if not title:
        return False
    # Claude Code 只运行在终端窗口中
    terminal_classes = ("CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass", "mintty")
    if class_name not in terminal_classes:
        return False
    # 终端窗口：标题以 Claude Code 特征字符开头
    # ✳ = 空闲, · = 空闲(变体), SPINNER_CHARS = 思考中
    first_char = title[0] if title else ""
    claude_prefix_chars = {"✳", "·", "•", "∙"}  # 各种可能的空闲前缀
    if first_char in claude_prefix_chars or first_char in SPINNER_CHARS:
        return True
    # 终端窗口标题包含 claude
    if "claude" in title.lower():
        return True
    return False


def _is_windsurf_window(title: str, class_name: str) -> bool:
    """判断窗口是否为 Windsurf IDE 窗口（排除浏览器）。"""
    if not title:
        return False
    # 必须是 Electron 窗口类
    if class_name != "Chrome_WidgetWin_1":
        return False
    title_lower = title.lower()
    # 排除浏览器窗口（Edge/Chrome 标题通常包含这些关键词）
    browser_keywords = ("edge", "chrome", "firefox", "页面", "标签页", "- 个人", "- 工作")
    for kw in browser_keywords:
        if kw in title_lower:
            return False
    # Windsurf IDE 标题格式: "项目名 - Windsurf" 或 "项目名 - Windsurf - 文件名"
    if "windsurf" in title_lower:
        return True
    return False


def detect_windsurf_state(title: str) -> str:
    """检测 Windsurf 窗口状态。"""
    if not title:
        return "unknown"
    # Windsurf 标题通常为: "项目名 - Windsurf - 账号信息"
    if "windsurf" in title.lower():
        return "idle"
    return "unknown"


def find_claude_windows() -> list[dict]:
    global _windows_cache, _windows_cache_time
    if time.time() - _windows_cache_time < 5:
        return _windows_cache
    
    results = []
    try:
        # 使用 win32 后端更稳定，UIA 后端可能卡死
        desktop = Desktop(backend="win32")
        for w in desktop.windows():
            try:
                handle = w.handle
                if not win32gui.IsWindow(handle):
                    continue
                title = w.window_text()
                cls = w.class_name()
                if _is_claude_window(title, cls):
                    st = detect_claude_state(title)
                    label = state["window_labels"].get(handle, "")
                    results.append({
                        "title": title,
                        "handle": handle,
                        "class": cls,
                        "state": st,
                        "label": label,
                        "type": "claude",
                    })
                elif _is_windsurf_window(title, cls):
                    st = detect_windsurf_state(title)
                    label = state["window_labels"].get(handle, "")
                    results.append({
                        "title": title,
                        "handle": handle,
                        "class": cls,
                        "state": st,
                        "label": label,
                        "type": "windsurf",
                    })
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"窗口扫描失败: {e}")
        return _windows_cache if _windows_cache else []
    
    order = {"idle": 0, "thinking": 1, "unknown": 2}
    type_order = {"claude": 0, "windsurf": 1}
    results.sort(key=lambda x: (type_order.get(x.get("type", ""), 9), order.get(x["state"], 9), -x["handle"]))
    _windows_cache = results
    _windows_cache_time = time.time()
    return results
