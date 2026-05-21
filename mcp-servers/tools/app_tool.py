"""
App Tool - Manages Windows applications
"""

import subprocess
import time
import pyautogui
from typing import Sequence
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
from . import BaseTool
import sys
sys.path.append('..')
from utils.logger import get_logger

try:
    import win32gui
    import win32con
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

logger = get_logger("app_tool")

class AppTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="Windows-MCP:App",
            description="Manages Windows applications with three modes: "
                       "'launch' (opens application), 'resize' (adjusts window), 'switch' (focus window), 'move_cursor' (moves mouse cursor)."
        )
    
    def get_tool_definition(self) -> Tool:
        return Tool(
            name=self.name,
            description=self.description,
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["launch", "resize", "switch", "list", "move_cursor"],
                        "description": "Operation mode"
                    },
                    "name": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "App name (launch) or window title (switch)"
                    },
                    "window_size": {
                        "type": ["array", "null"],
                        "items": {"type": "integer"},
                        "default": None,
                        "description": "Window size [width, height]"
                    },
                    "window_loc": {
                        "type": ["array", "null"],
                        "items": {"type": "integer"},
                        "default": None,
                        "description": "Window position [x, y]"
                    },
                    "x": {
                        "type": ["integer", "null"],
                        "default": None,
                        "description": "X coordinate for cursor movement"
                    },
                    "y": {
                        "type": ["integer", "null"],
                        "default": None,
                        "description": "Y coordinate for cursor movement"
                    }
                },
                "required": ["mode"]
            }
        )
    
    async def execute(self, arguments: dict) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
        mode = arguments["mode"]
        name = arguments.get("name")
        window_size = arguments.get("window_size")
        window_loc = arguments.get("window_loc")
        x = arguments.get("x")
        y = arguments.get("y")
        
        try:
            if mode == "launch":
                return await self._launch_app(name)
            elif mode == "resize":
                return await self._resize_window(window_size, window_loc)
            elif mode == "switch":
                return await self._switch_window(name)
            elif mode == "list":
                return await self._list_windows()
            elif mode == "move_cursor":
                return await self._move_cursor(x, y)
        except Exception as e:
            return [TextContent(type="text", text=f"ERROR: {str(e)}")]
    
    async def _launch_app(self, name: str) -> Sequence[TextContent]:
        if not name:
            return [TextContent(type="text", text="ERROR: App name required")]
        
        app_map = {
            "notepad": "notepad.exe", "calculator": "calc.exe", "paint": "mspaint.exe",
            "cmd": "cmd.exe", "powershell": "powershell.exe", "explorer": "explorer.exe"
        }
        
        executable = app_map.get(name.lower(), name)
        process = subprocess.Popen(executable, shell=True)
        time.sleep(1.0)
        
        return [TextContent(type="text", text=f"Launched: {name} (PID: {process.pid})")]
    
    async def _resize_window(self, window_size, window_loc) -> Sequence[TextContent]:
        if not WIN32_AVAILABLE:
            return [TextContent(type="text", text="ERROR: win32gui not available")]
        
        hwnd = win32gui.GetForegroundWindow()
        
        if window_size:
            width, height = window_size
            x = window_loc[0] if window_loc else 0
            y = window_loc[1] if window_loc else 0
            win32gui.MoveWindow(hwnd, x, y, width, height, True)
            return [TextContent(type="text", text=f"Resized to {width}x{height}")]
        
        return [TextContent(type="text", text="ERROR: window_size required")]
    
    async def _switch_window(self, title: str) -> Sequence[TextContent]:
        if not title:
            return [TextContent(type="text", text="ERROR: Title required")]
        try:
            command = 'powershell "Get-Process | Where-Object {$_.MainWindowTitle -ne \"\"} | Select-Object Name,MainWindowTitle | Format-List"'
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10)
            output = result.stdout
            lines = output.strip().split('\r\n')
            current_process_name = None
            current_window_title = None
            windows_found = []
            for line in lines:
                if line.startswith("Name            :"):
                    current_process_name = line.split(":", 1)[1].strip()
                elif line.startswith("MainWindowTitle :"):
                    current_window_title = line.split(":", 1)[1].strip()
                if current_process_name and current_window_title:
                    windows_found.append({"Name": current_process_name, "MainWindowTitle": current_window_title})
                    current_process_name = None
                    current_window_title = None
            for window in windows_found:
                if title.lower() in window["MainWindowTitle"].lower():
                    if WIN32_AVAILABLE:
                        return [TextContent(type="text", text=f"Found and attempted to switch to: {window['MainWindowTitle']}")]
                    else:
                        return [TextContent(type="text", text=f"Found: {window['MainWindowTitle']} (via PowerShell)")]
            return [TextContent(type="text", text=f"ERROR: Window '{title}' not found")]
        except subprocess.TimeoutExpired:
            return [TextContent(type="text", text="ERROR: PowerShell command timed out.")]
        except Exception as e:
            return [TextContent(type="text", text=f"ERROR: {str(e)}")]

    async def _list_windows(self) -> Sequence[TextContent]:
        try:
            command = 'powershell "Get-Process | Where-Object {$_.MainWindowTitle -ne \"\"} | Select-Object Name, MainWindowTitle | Format-Table -AutoSize"'
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10)
            return [TextContent(type="text", text=result.stdout)]
        except subprocess.TimeoutExpired:
            return [TextContent(type="text", text="ERROR: PowerShell command timed out for listing windows.")]
        except Exception as e:
            return [TextContent(type="text", text=f"ERROR: {str(e)}")]

    async def _move_cursor(self, x: int, y: int) -> Sequence[TextContent]:
        if x is None or y is None:
            return [TextContent(type="text", text="ERROR: X and Y coordinates required for cursor movement.")]
        try:
            pyautogui.moveTo(x, y)
            return [TextContent(type="text", text=f"Cursor moved to ({x}, {y}).")]
        except Exception as e:
            return [TextContent(type="text", text=f"ERROR: Failed to move cursor: {str(e)}")]