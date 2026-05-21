"""
Window Tool - Window management operations
"""

from typing import Sequence
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
from . import BaseTool
import sys
sys.path.append('..')
from utils.logger import get_logger
from utils.accessibility import get_active_window_info

logger = get_logger("window_tool")

class WindowTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="Windows-MCP:Window",
            description="Manages windows: list, focus, or get active window."
        )
    
    def get_tool_definition(self) -> Tool:
        return Tool(
            name=self.name,
            description=self.description,
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "focus", "active"],
                        "description": "Action: 'list' all windows, 'focus' window by title, 'active' get current window"
                    },
                    "title": {
                        "type": "string",
                        "description": "Window title (partial match) for 'focus' action"
                    }
                },
                "required": ["action"]
            }
        )
    
    async def execute(self, arguments: dict) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
        is_valid, error = self.validate_arguments(arguments, self.get_tool_definition().inputSchema)
        if not is_valid:
            return [TextContent(type="text", text=f"ERROR: {error}")]
        
        action = arguments["action"]
        
        try:
            if action == "list":
                import subprocess
                command = 'powershell "Get-Process | Where-Object {$_.MainWindowTitle -ne \"\"} | Select-Object Name,MainWindowTitle | Format-Table -AutoSize"'
                result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10)
                output = result.stdout
                logger.info(f"Listed windows via PowerShell")
                return [TextContent(type="text", text=output)]
                
            elif action == "active":
                active = get_active_window_info()
                result = f"Active: {active['title']}\nPosition: ({active['position']['x']}, {active['position']['y']})\nSize: {active['position']['width']}x{active['position']['height']}"
                logger.info(f"Got active window: {active['title']}")
                return [TextContent(type="text", text=result)]
                
            elif action == "focus":
                return [TextContent(type="text", text="ERROR: 'focus' action is temporarily disabled due to underlying library changes. Please use 'AppTool's switch mode if available.")]
                    
        except Exception as e:
            logger.error(f"Window operation failed: {e}", exc_info=True)
            return [TextContent(type="text", text=f"ERROR: {str(e)}")]
