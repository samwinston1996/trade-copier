"""Called as a subprocess to send MT5 orders — bypasses rpyc RPC issues."""
import sys, json
from mt5linux import MetaTrader5

order_json, login, password, server = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
order = json.loads(order_json)

c = MetaTrader5(host="localhost", port=8001)
c.initialize(login=int(login), password=password, server=server)
c.symbol_select(order["symbol"], True)
r = c.order_send(order)
if r:
    print(f"OK:{r.retcode}:{r.order}")
else:
    # Capture MT5's last_error before giving up
    err = c.last_error() if hasattr(c, 'last_error') else ("unknown", "N/A")
    print(f"ERR:None:order_send returned None — last_error={err}")