from typing import Dict, List


class ComService:
    def list_ports(self) -> List[Dict[str, str]]:
        try:
            from serial.tools import list_ports
        except ImportError as exc:
            raise RuntimeError("pyserial is not installed") from exc

        ports: List[Dict[str, str]] = []
        for port in list_ports.comports():
            ports.append(
                {
                    "device": port.device,
                    "description": port.description or "",
                    "hwid": port.hwid or "",
                }
            )

        return ports


com_service = ComService()
