"""
Snapshot Tool - Captures desktop screenshot using PIL
"""

import base64
import io
from typing import Sequence
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
from . import BaseTool


class SnapshotTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="Windows-MCP:Snapshot",
            description="Captures screenshot of the desktop and returns it as an image."
        )

    def get_tool_definition(self) -> Tool:
        return Tool(
            name=self.name,
            description=self.description,
            inputSchema={
                "type": "object",
                "properties": {
                    "use_vision": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include screenshot (default: True)"
                    },
                    "use_dom": {
                        "type": "boolean",
                        "default": False,
                        "description": "Legacy parameter, ignored"
                    }
                },
                "required": []
            }
        )

    async def execute(self, arguments: dict) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
        try:
            from PIL import ImageGrab

            # Capture screenshot directly via PIL
            screenshot = ImageGrab.grab()

            # Convert to base64 PNG
            buffer = io.BytesIO()
            screenshot.save(buffer, format="PNG")
            img_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

            width, height = screenshot.size

            return [
                ImageContent(type="image", data=img_base64, mimeType="image/png"),
                TextContent(type="text", text=f"Screenshot captured: {width}x{height} pixels.")
            ]

        except ImportError:
            # Fallback: use PowerShell if PIL not available
            try:
                import subprocess
                ps_cmd = (
                    "Add-Type -AssemblyName System.Drawing,System.Windows.Forms;"
                    "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                    "$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height);"
                    "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                    "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
                    "$ms=New-Object System.IO.MemoryStream;"
                    "$bmp.Save($ms,[System.Drawing.Imaging.ImageFormat]::Png);"
                    "[System.Convert]::ToBase64String($ms.ToArray())"
                )
                r = subprocess.run(
                    ["powershell", "-Command", ps_cmd],
                    capture_output=True, text=True, timeout=15
                )
                img_base64 = r.stdout.strip()
                if not img_base64:
                    raise Exception(r.stderr or "Empty output from PowerShell")
                return [
                    ImageContent(type="image", data=img_base64, mimeType="image/png"),
                    TextContent(type="text", text="Screenshot captured via PowerShell fallback.")
                ]
            except Exception as e2:
                return [TextContent(type="text", text=f"Screenshot failed (fallback): {e2}")]

        except Exception as e:
            return [TextContent(type="text", text=f"Screenshot failed: {e}")]
