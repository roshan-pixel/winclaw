"""
Network Manager Tool - Network operations and monitoring
"""

import subprocess
import urllib.request
from typing import Sequence
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
from .. import BaseTool
import sys
sys.path.append('../..')
from utils.logger import get_logger

logger = get_logger("network_manager_tool")


class NetworkManagerTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="Windows-MCP:NetworkManager",
            description="Network operations: public_ip, local_ip, connections, port info, dns_flush, netstat, ipconfig, ping."
        )

    def get_tool_definition(self):
        return Tool(
            name=self.name,
            description=self.description,
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["public_ip", "local_ip", "status", "connections", "dns_flush", "netstat", "ipconfig", "ping"],
                        "description": "Action to perform"
                    },
                    "target": {
                        "type": "string",
                        "description": "Target host for ping"
                    }
                },
                "required": ["action"]
            }
        )

    async def execute(self, arguments):
        action = arguments.get("action", "status")
        target = arguments.get("target")

        try:
            if action == "public_ip":
                # Try multiple fast endpoints with 5s timeout
                for url in ["https://api.ipify.org?format=text", "https://ifconfig.me/ip"]:
                    try:
                        req = urllib.request.urlopen(url, timeout=5)
                        ip = req.read().decode().strip()
                        if ip:
                            return [TextContent(type="text", text=f"Public IP: {ip}")]
                    except Exception:
                        continue
                return [TextContent(type="text", text="Could not determine public IP (timeout or no internet)")]

            elif action == "local_ip":
                r = subprocess.run(
                    ["powershell", "-Command",
                     "Get-NetIPAddress -AddressFamily IPv4 | "
                     "Where-Object {$_.IPAddress -notlike '169.*' -and $_.IPAddress -ne '127.0.0.1'} | "
                     "Select-Object IPAddress, InterfaceAlias | Format-Table -AutoSize"],
                    capture_output=True, text=True, timeout=8
                )
                return [TextContent(type="text", text=r.stdout or "No local IP found")]

            elif action == "status":
                # Return both public and local IP + connectivity check
                results = []

                # Public IP
                public_ip = "unavailable"
                for url in ["https://api.ipify.org?format=text", "https://ifconfig.me/ip"]:
                    try:
                        req = urllib.request.urlopen(url, timeout=5)
                        public_ip = req.read().decode().strip()
                        break
                    except Exception:
                        continue
                results.append(f"Public IP  : {public_ip}")

                # Local IP
                r = subprocess.run(
                    ["powershell", "-Command",
                     "Get-NetIPAddress -AddressFamily IPv4 | "
                     "Where-Object {$_.IPAddress -notlike '169.*' -and $_.IPAddress -ne '127.0.0.1'} | "
                     "Select-Object -ExpandProperty IPAddress"],
                    capture_output=True, text=True, timeout=8
                )
                local_ip = r.stdout.strip().split("\n")[0].strip() if r.stdout.strip() else "unavailable"
                results.append(f"Local IP   : {local_ip}")

                # Internet check
                try:
                    urllib.request.urlopen("https://www.google.com", timeout=4)
                    results.append("Internet   : ✅ Connected")
                except Exception:
                    results.append("Internet   : ❌ No connection")

                return [TextContent(type="text", text="\n".join(results))]

            elif action == "connections":
                result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10)
                return [TextContent(type="text", text=result.stdout[:3000])]

            elif action == "dns_flush":
                result = subprocess.run(["ipconfig", "/flushdns"], capture_output=True, text=True, timeout=10)
                return [TextContent(type="text", text=result.stdout)]

            elif action == "netstat":
                result = subprocess.run(["netstat", "-s"], capture_output=True, text=True, timeout=10)
                return [TextContent(type="text", text=result.stdout[:3000])]

            elif action == "ipconfig":
                result = subprocess.run(["ipconfig", "/all"], capture_output=True, text=True, timeout=10)
                return [TextContent(type="text", text=result.stdout[:3000])]

            elif action == "ping":
                if not target:
                    return [TextContent(type="text", text="ERROR: target required for ping")]
                result = subprocess.run(["ping", "-n", "4", target], capture_output=True, text=True, timeout=20)
                return [TextContent(type="text", text=result.stdout)]

            else:
                return [TextContent(type="text", text=f"Unknown action: {action}")]

        except subprocess.TimeoutExpired:
            return [TextContent(type="text", text=f"ERROR: Command timed out for action '{action}'")]
        except Exception as e:
            return [TextContent(type="text", text=f"ERROR: {str(e)}")]
