from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

from linkstate.config import NodeConfig
from linkstate.node import Node
from linkstate.banking import ATMClient, ATMServer



def main() -> None:
    configured = os.environ.get("SMOKE_ROUTERS", "u,x,y,z")
    router_ids = [node_id.strip() for node_id in configured.split(",") if node_id.strip()]
    routers = []
    for node_id in router_ids:
        cfg = NodeConfig.load(ROOT / "configs" / f"{node_id}.json")
        node = Node(cfg, table_dir=ROOT / "tables", verbose=False)
        node.start()
        routers.append(node)

    bank_config_name = os.environ.get("SMOKE_BANK_CONFIG", "bank.json")
    bank_id = os.environ.get("SMOKE_BANK_ID", "servidor_bancario")
    bank_cfg = NodeConfig.load(ROOT / "configs" / bank_config_name)
    atm_config_name = os.environ.get("SMOKE_ATM_CONFIG", "atm.json")
    atm_cfg = NodeConfig.load(ROOT / "configs" / atm_config_name)

    bank = ATMServer(bank_cfg)
    bank_thread = threading.Thread(target=bank.start, daemon=True)
    bank_thread.start()

    deadline = time.monotonic() + float(os.environ.get("SMOKE_CONVERGENCE_TIMEOUT", "45"))
    while time.monotonic() < deadline:
        routes_to_bank = all(
            node.control is not None and node.control.lookup(bank_id) is not None
            for node in routers
        )
        routes_to_atm = all(
            node.control is not None and node.control.lookup(atm_cfg.node_id) is not None
            for node in routers
        )
        if routes_to_bank and routes_to_atm:
            break
        time.sleep(0.5)
    else:
        status = {
            node.cfg.node_id: {
                "bank": node.control.lookup(bank_id) is not None if node.control else False,
                "atm": node.control.lookup(atm_cfg.node_id) is not None if node.control else False,
            }
            for node in routers
        }
        raise RuntimeError(f"convergencia incompleta: {status}")

    atm = ATMClient(atm_cfg, bank_id=bank_id)
    atm.start_listener()
    try:
        login = atm.request("login", {"tarjeta": "22523", "pin": "4444"})
        assert login["action"] == "login_ok", login
        withdrawal = atm.request("retiro", {"cantidad": 100.0})
        assert withdrawal["action"] == "retiro_ok", withdrawal
        logout = atm.request("logout", {})
        assert logout["action"] == "logout_ok", logout
        print("SMOKE_TEST_OK")
    finally:
        atm.stop()
        bank.stop()
        for node in routers:
            node.stop()


if __name__ == "__main__":
    main()
