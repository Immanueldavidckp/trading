import os
import sys
import time
import json
import pyotp
import hashlib
import requests
import random
from datetime import datetime, timedelta

# Default Mock Data File
MOCK_DATA_FILE = os.path.join(os.path.dirname(__file__), "mock_portfolio.json")

class ShoonyaClient:
    def __init__(self, config=None):
        self.config = config or {}
        self.session_token = None
        self.actid = None
        self.uname = None
        self.api_url = "https://api.shoonya.com/NorenWClientTP"
        
        # Determine mode
        self.mock_mode = self.config.get("mock_mode", True)
        
        # Load mock portfolio database
        self._load_mock_db()

    def _load_mock_db(self):
        if not self.mock_mode:
            return
            
        if os.path.exists(MOCK_DATA_FILE):
            try:
                with open(MOCK_DATA_FILE, "r") as f:
                    self.mock_db = json.load(f)
            except Exception:
                self._init_empty_mock_db()
        else:
            self._init_empty_mock_db()

    def _init_empty_mock_db(self):
        self.mock_db = {
            "orders": [],
            "positions": {},
            "holdings": [
                {
                    "exch": "NSE",
                    "token": "2885",
                    "tsym": "RELIANCE-EQ",
                    "qty": 50,
                    "avgprc": 2180.00,
                    "cname": "RELIANCE INDUSTRIES LTD"
                },
                {
                    "exch": "NSE",
                    "token": "1594",
                    "tsym": "INFY-EQ",
                    "qty": 100,
                    "avgprc": 1420.50,
                    "cname": "INFOSYS LIMITED"
                }
            ],
            "balance": 1000000.0  # Initial 10 Lakhs paper balance
        }
        self._save_mock_db()

    def _save_mock_db(self):
        if not self.mock_mode:
            return
        try:
            with open(MOCK_DATA_FILE, "w") as f:
                json.dump(self.mock_db, f, indent=4)
        except Exception as e:
            print(f"Error saving mock database: {e}")

    def get_hash(self, data):
        return hashlib.sha256(data.encode('utf-8')).hexdigest()

    def get_totp(self, secret):
        secret = secret.replace(' ', '')
        totp = pyotp.TOTP(secret)
        return totp.now()

    def login(self, user_id=None, password=None, vendor_code=None, api_key=None, totp_secret=None, imei=None):
        """Authenticates with Shoonya API using credentials"""
        
        # Override config if provided
        uid = user_id or self.config.get("user_id")
        pwd = password or self.config.get("password")
        vc = vendor_code or self.config.get("vendor_code")
        key = api_key or self.config.get("api_key")
        totp_sec = totp_secret or self.config.get("totp_secret")
        device_imei = imei or self.config.get("imei", "abc1234")

        # Update client config
        self.config.update({
            "user_id": uid,
            "password": pwd,
            "vendor_code": vc,
            "api_key": key,
            "totp_secret": totp_sec,
            "imei": device_imei
        })

        if self.mock_mode:
            self.session_token = "mock_session_token_12345"
            self.actid = uid or "FA153046"
            self.uname = "PAPER TRADER (MOCK)"
            return {
                "stat": "Ok",
                "susertoken": self.session_token,
                "uname": self.uname,
                "actid": self.actid,
                "email": "paper_trader@example.com",
                "brkname": "PAPER_BROKER"
            }

        if not all([uid, pwd, vc, key, totp_sec]):
            return {"stat": "Not_Ok", "emsg": "Missing Shoonya credentials. Please verify your settings."}

        print(f"Connecting to Shoonya API for user: {uid}...")
        password_hash = self.get_hash(pwd)
        appkey_str = f"{uid}|{key}"
        appkey_hash = self.get_hash(appkey_str)
        
        try:
            totp_val = self.get_totp(totp_sec)
        except Exception as e:
            return {"stat": "Not_Ok", "emsg": f"Failed to generate TOTP: {e}. Check if TOTP Secret is correct."}

        payload = {
            "apkversion": "1.0.0",
            "uid": uid,
            "pwd": password_hash,
            "factor2": totp_val,
            "vc": vc,
            "appkey": appkey_hash,
            "imei": device_imei,
            "source": "API"
        }

        url = f"{self.api_url}/QuickAuth"
        try:
            response = requests.post(url, data={"jData": json.dumps(payload)}, timeout=10)
            if response.status_code == 200:
                res_json = response.json()
                if res_json.get("stat") == "Ok":
                    self.session_token = res_json.get("susertoken")
                    self.actid = res_json.get("actid", uid)
                    self.uname = res_json.get("uname", "SHONNYA USER")
                    return res_json
                else:
                    return {"stat": "Not_Ok", "emsg": res_json.get("emsg", "Login failed")}
            else:
                return {"stat": "Not_Ok", "emsg": f"Server error: {response.status_code}"}
        except requests.exceptions.Timeout:
            return {"stat": "Not_Ok", "emsg": "Connection timed out. Enable 'Paper Trading' to continue offline."}
        except Exception as e:
            return {"stat": "Not_Ok", "emsg": f"Network Error: {str(e)}"}

    def _call_api(self, endpoint, payload):
        if self.mock_mode:
            return {"stat": "Not_Ok", "emsg": "API called in mock mode"}

        if not self.session_token:
            login_res = self.login()
            if login_res.get("stat") != "Ok":
                return login_res

        url = f"{self.api_url}/{endpoint}"
        jData = json.dumps(payload)
        jKey = f"{self.config.get('user_id')}|{self.session_token}"
        data = {"jData": jData, "jKey": jKey}

        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            response = requests.post(url, data=data, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            else:
                return {"stat": "Not_Ok", "emsg": f"HTTP Error {response.status_code}"}
        except Exception as e:
            return {"stat": "Not_Ok", "emsg": str(e)}

    def search_scrip(self, exchange, search_text):
        """Search scrips by text"""
        if self.mock_mode:
            # Predefined mock stocks
            mock_scrips = [
                {"exch": "NSE", "token": "2885", "tsym": "RELIANCE-EQ", "cname": "RELIANCE INDUSTRIES LTD", "lp": "2194.20"},
                {"exch": "NSE", "token": "1594", "tsym": "INFY-EQ", "cname": "INFOSYS LIMITED", "lp": "1420.50"},
                {"exch": "NSE", "token": "11536", "tsym": "TCS-EQ", "cname": "TATA CONSULTANCY SERV LT", "lp": "3850.10"},
                {"exch": "NSE", "token": "3045", "tsym": "SBIN-EQ", "cname": "STATE BANK OF INDIA", "lp": "742.80"},
                {"exch": "NSE", "token": "1333", "tsym": "HDFCBANK-EQ", "cname": "HDFC BANK LIMITED", "lp": "1480.20"},
                {"exch": "NSE", "token": "3456", "tsym": "TATAMOTORS-EQ", "cname": "TATA MOTORS LIMITED", "lp": "965.40"},
                {"exch": "NSE", "token": "26000", "tsym": "NIFTY-50", "cname": "NIFTY 50 INDEX", "lp": "22450.00"}
            ]
            matches = []
            for scrip in mock_scrips:
                if (search_text.upper() in scrip["tsym"]) or (search_text.upper() in scrip["cname"].upper()):
                    if not exchange or scrip["exch"] == exchange.upper():
                        matches.append(scrip)
            return {"stat": "Ok", "values": matches}

        payload = {
            "uid": self.config.get("user_id"),
            "stext": search_text,
            "exch": exchange
        }
        res = self._call_api("SearchScrip", payload)
        
        # Check if output is list directly or values dict
        if isinstance(res, list):
            return {"stat": "Ok", "values": res}
        return res

    def get_quotes(self, exchange, token):
        """Fetch stock quote"""
        if self.mock_mode:
            # Predefined base values
            mock_prices = {
                "2885": {"tsym": "RELIANCE-EQ", "lp": 2194.20, "cname": "RELIANCE INDUSTRIES LTD", "c": 2176.60},
                "1594": {"tsym": "INFY-EQ", "lp": 1420.50, "cname": "INFOSYS LIMITED", "c": 1435.00},
                "11536": {"tsym": "TCS-EQ", "lp": 3850.10, "cname": "TATA CONSULTANCY SERV LT", "c": 3810.00},
                "3045": {"tsym": "SBIN-EQ", "lp": 742.80, "cname": "STATE BANK OF INDIA", "c": 738.00},
                "1333": {"tsym": "HDFCBANK-EQ", "lp": 1480.20, "cname": "HDFC BANK LIMITED", "c": 1495.00},
                "3456": {"tsym": "TATAMOTORS-EQ", "lp": 965.40, "cname": "TATA MOTORS LIMITED", "c": 948.00},
                "26000": {"tsym": "NIFTY-50", "lp": 22450.00, "cname": "NIFTY 50 INDEX", "c": 22400.00}
            }
            
            scrip = mock_prices.get(token, {"tsym": f"TOKEN-{token}", "lp": 100.0, "cname": "Mock Scrip Ltd", "c": 100.0})
            
            # Add a slight random drift (-0.5% to +0.5%) to make it look alive
            drift_pct = random.uniform(-0.002, 0.002)
            scrip["lp"] = round(scrip["lp"] * (1 + drift_pct), 2)
            
            # Calculate change details
            lp = scrip["lp"]
            close = scrip["c"]
            change = round(lp - close, 2)
            pct_change = round((change / close) * 100, 2)
            
            return {
                "stat": "Ok",
                "exch": exchange,
                "tsym": scrip["tsym"],
                "token": token,
                "lp": str(lp),
                "c": str(close),
                "o": str(round(close * 0.99, 2)),
                "h": str(round(max(lp, close) * 1.01, 2)),
                "l": str(round(min(lp, close) * 0.98, 2)),
                "v": str(random.randint(100000, 5000000)),
                "pc": str(pct_change),
                "cname": scrip["cname"]
            }

        payload = {
            "uid": self.config.get("user_id"),
            "exch": exchange,
            "token": token
        }
        return self._call_api("GetQuotes", payload)

    def get_candles(self, exchange, token, interval=5, days=3):
        """Fetch historical candle data"""
        if self.mock_mode:
            # Generate mock candlesticks
            now = datetime.now()
            candles = []
            
            # Base price
            base_quotes = {
                "2885": 2194.20,
                "1594": 1420.50,
                "11536": 3850.10,
                "3045": 742.80,
                "1333": 1480.20,
                "3456": 965.40,
                "26000": 22450.00
            }
            price = base_quotes.get(token, 100.0)
            
            # We want around 100-200 periods
            periods = 100
            time_delta = timedelta(minutes=interval)
            
            for i in range(periods, 0, -1):
                candle_time = now - (i * time_delta)
                # Formulate a nice random walk with slightly positive trend
                change = price * random.uniform(-0.005, 0.006)
                o = price
                c = price + change
                h = max(o, c) + (price * random.uniform(0.0, 0.003))
                l = min(o, c) - (price * random.uniform(0.0, 0.003))
                v = random.randint(5000, 100000)
                
                # Keep values realistic
                o, h, l, c = round(o, 2), round(h, 2), round(l, 2), round(c, 2)
                
                candles.append({
                    "time": candle_time.strftime("%d-%m-%Y %H:%M:%S"),
                    "into": str(o),
                    "inth": str(h),
                    "intl": str(l),
                    "intc": str(c),
                    "v": str(v)
                })
                
                price = c
                
            return {"stat": "Ok", "candles": candles}

        # For live mode: calculate start and end unix timestamps
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)
        
        st = int(time.mktime(start_dt.timetuple()))
        et = int(time.mktime(end_dt.timetuple()))
        
        payload = {
            "uid": self.config.get("user_id"),
            "exch": exchange,
            "token": token,
            "starttime": str(st),
            "endtime": str(et),
            "interval": str(interval)
        }
        
        # TimePriceSeries URL is separate: https://api.shoonya.com/chartapi/getdata/
        url = "https://api.shoonya.com/chartapi/getdata/"
        jKey = f"{self.config.get('user_id')}|{self.session_token}"
        data = {
            "jData": json.dumps(payload),
            "jKey": jKey
        }
        
        try:
            response = requests.post(url, data=data, timeout=10)
            if response.status_code == 200:
                res_json = response.json()
                # Check for standard API list response
                if isinstance(res_json, list):
                    return {"stat": "Ok", "candles": res_json}
                elif isinstance(res_json, dict) and res_json.get("stat") == "Ok":
                    # Convert to standard format
                    return res_json
                else:
                    return {"stat": "Not_Ok", "emsg": res_json.get("emsg", "Failed to fetch chart data.")}
            else:
                return {"stat": "Not_Ok", "emsg": f"HTTP Error {response.status_code}"}
        except Exception as e:
            return {"stat": "Not_Ok", "emsg": str(e)}

    def place_order(self, exchange, tsym, qty, prc, trantype, prctyp, prd, token=None):
        """Place order"""
        if self.mock_mode:
            # Paper Order Execution
            order_id = f"MOCKORD{random.randint(100000, 999999)}"
            order_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
            
            # Determine execution price
            exec_price = float(prc)
            if prctyp == "MKT":
                # Get current LTP
                scrip_token = token or "2885"  # default RELIANCE
                quote = self.get_quotes(exchange, scrip_token)
                exec_price = float(quote.get("lp", prc))

            order = {
                "norenordno": order_id,
                "tsym": tsym,
                "exch": exchange,
                "qty": str(qty),
                "prc": str(prc),
                "trantype": trantype, # B / S
                "prctyp": prctyp, # LMT / MKT
                "prd": prd, # I (Intraday) / C (Delivery)
                "status": "COMPLETE" if prctyp == "MKT" else "PENDING",
                "norenordno": order_id,
                "ordtime": order_time,
                "fillshares": str(qty) if prctyp == "MKT" else "0",
                "flprc": str(exec_price) if prctyp == "MKT" else "0.0",
                "token": token or "2885"
            }
            
            # Add order to database
            self.mock_db["orders"].insert(0, order)
            
            # If order is complete, update positions / balance / holdings
            if order["status"] == "COMPLETE":
                self._update_mock_portfolio(order)
                
            self._save_mock_db()
            
            return {
                "stat": "Ok",
                "norenordno": order_id,
                "emsg": "Mock Order executed successfully."
            }

        # Live Order Placement
        payload = {
            "uid": self.config.get("user_id"),
            "actid": self.actid or self.config.get("user_id"),
            "exch": exchange,
            "tsym": tsym,
            "qty": str(qty),
            "dscqty": "0",
            "prc": str(prc),
            "prd": prd,
            "trantype": trantype,
            "prctyp": prctyp,
            "ret": "DAY"
        }
        return self._call_api("PlaceOrder", payload)

    def cancel_order(self, orderno):
        """Cancel order"""
        if self.mock_mode:
            for order in self.mock_db["orders"]:
                if order["norenordno"] == orderno:
                    if order["status"] == "PENDING":
                        order["status"] = "CANCELLED"
                        self._save_mock_db()
                        return {"stat": "Ok", "result": orderno}
                    else:
                        return {"stat": "Not_Ok", "emsg": f"Order already {order['status']}"}
            return {"stat": "Not_Ok", "emsg": "Order not found"}

        payload = {
            "uid": self.config.get("user_id"),
            "orderno": orderno
        }
        return self._call_api("CancelOrder", payload)

    def get_order_book(self):
        """Get placed orders"""
        if self.mock_mode:
            return self.mock_db["orders"]
            
        payload = {
            "uid": self.config.get("user_id")
        }
        res = self._call_api("OrderBook", payload)
        if isinstance(res, list):
            return res
        return []

    def get_positions(self):
        """Get net positions"""
        if self.mock_mode:
            # Return list of positions
            pos_list = []
            for token, pos in self.mock_db["positions"].items():
                # Fetch quote for LTP updating
                quote = self.get_quotes(pos["exch"], token)
                ltp = float(quote.get("lp", pos["avgprc"]))
                avgprc = float(pos["avgprc"])
                netqty = int(pos["qty"])
                
                # Calculate MTOM (Mark to market)
                urmtom = round((ltp - avgprc) * netqty, 2)
                
                pos_list.append({
                    "exch": pos["exch"],
                    "token": token,
                    "tsym": pos["tsym"],
                    "netqty": str(netqty),
                    "avgprc": str(avgprc),
                    "lp": str(ltp),
                    "urmtom": str(urmtom),
                    "prd": pos["prd"]
                })
            return pos_list

        payload = {
            "uid": self.config.get("user_id")
        }
        res = self._call_api("PositionBook", payload)
        if isinstance(res, list):
            return res
        return []

    def get_holdings(self):
        """Get stock holdings"""
        if self.mock_mode:
            holdings_list = []
            for hld in self.mock_db["holdings"]:
                # Fetch quote for LTP update
                quote = self.get_quotes(hld["exch"], hld["token"])
                ltp = float(quote.get("lp", hld["avgprc"]))
                avgprc = float(hld["avgprc"])
                qty = int(hld["qty"])
                
                # MTM
                total_val = round(ltp * qty, 2)
                avg_val = round(avgprc * qty, 2)
                pnl = round(total_val - avg_val, 2)
                pnl_pct = round((pnl / avg_val) * 100, 2) if avg_val > 0 else 0.0

                holdings_list.append({
                    "exch": hld["exch"],
                    "token": hld["token"],
                    "tsym": hld["tsym"],
                    "qty": str(qty),
                    "avgprc": str(avgprc),
                    "lp": str(ltp),
                    "pnl": str(pnl),
                    "pnl_pct": str(pnl_pct),
                    "cname": hld["cname"]
                })
            return holdings_list

        payload = {
            "uid": self.config.get("user_id"),
            "actid": self.actid or self.config.get("user_id")
        }
        res = self._call_api("Holdings", payload)
        if isinstance(res, list):
            return res
        return []

    def _update_mock_portfolio(self, order):
        token = order["token"]
        qty = int(order["qty"])
        price = float(order["flprc"])
        trantype = order["trantype"]
        tsym = order["tsym"]
        exch = order["exch"]
        prd = order["prd"]
        
        cost = qty * price
        
        # Position logic
        if trantype == "B":
            # Check balance
            if self.mock_db["balance"] < cost:
                # Cancel order because of insufficient funds
                order["status"] = "REJECTED"
                order["emsg"] = "Insufficient funds in paper trading balance."
                return
            
            # Deduct balance
            self.mock_db["balance"] -= cost
            
            # Check positions
            if token in self.mock_db["positions"]:
                pos = self.mock_db["positions"][token]
                old_qty = pos["qty"]
                old_avg = pos["avgprc"]
                
                new_qty = old_qty + qty
                # Weighted average price
                new_avg = round(((old_qty * old_avg) + cost) / new_qty, 2)
                
                pos["qty"] = new_qty
                pos["avgprc"] = new_avg
            else:
                self.mock_db["positions"][token] = {
                    "tsym": tsym,
                    "exch": exch,
                    "qty": qty,
                    "avgprc": price,
                    "prd": prd
                }
        elif trantype == "S":
            # Selling
            if token in self.mock_db["positions"]:
                pos = self.mock_db["positions"][token]
                old_qty = pos["qty"]
                
                # We can short in MIS intraday, but for Delivery let's enforce holdings check
                if prd == "C": # Delivery CNC
                    # Verify holdings
                    holding_found = False
                    for hld in self.mock_db["holdings"]:
                        if hld["token"] == token:
                            if hld["qty"] >= qty:
                                hld["qty"] -= qty
                                holding_found = True
                                # Clean up holding if 0
                                if hld["qty"] <= 0:
                                    self.mock_db["holdings"].remove(hld)
                                break
                    
                    if not holding_found:
                        order["status"] = "REJECTED"
                        order["emsg"] = "Insufficient holdings to execute sell order."
                        return

                new_qty = old_qty - qty
                # Add credit to balance
                self.mock_db["balance"] += cost
                
                if new_qty == 0:
                    del self.mock_db["positions"][token]
                else:
                    pos["qty"] = new_qty
            else:
                # If short selling intraday
                if prd == "I": # Intraday allowed
                    self.mock_db["balance"] += cost
                    self.mock_db["positions"][token] = {
                        "tsym": tsym,
                        "exch": exch,
                        "qty": -qty,
                        "avgprc": price,
                        "prd": prd
                    }
                else:
                    order["status"] = "REJECTED"
                    order["emsg"] = "Short selling only allowed in Intraday (MIS) product."
                    return
                    
        self._save_mock_db()

    def get_balance(self):
        """Return available margin / paper balance"""
        if self.mock_mode:
            return round(self.mock_db["balance"], 2)
            
        # For live trading, fetch limits
        payload = {
            "uid": self.config.get("user_id"),
            "actid": self.actid or self.config.get("user_id")
        }
        # Limit endpoint is /Limits
        res = self._call_api("Limits", payload)
        if isinstance(res, dict) and res.get("stat") == "Ok":
            # cash limit is usually cash or margin
            return float(res.get("cash", 0.0))
        return 0.0
