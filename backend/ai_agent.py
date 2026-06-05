import os
import json
import pandas as pd
import numpy as np
import google.generativeai as genai

class AIAgent:
    def __init__(self, gemini_api_key=None):
        self.api_key = gemini_api_key or os.environ.get("GEMINI_API_KEY")
        self.model_name = "gemini-1.5-flash"
        
        if self.api_key:
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel(self.model_name)
        else:
            self.model = None

    def set_api_key(self, api_key):
        self.api_key = api_key
        if api_key:
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel(self.model_name)
        else:
            self.model = None

    def calculate_indicators(self, candles_list):
        """
        Parses candlestick data and computes technical indicators:
        SMA-20, EMA-50, RSI-14, MACD (12, 26, 9)
        """
        if not candles_list or len(candles_list) < 15:
            # Not enough data for technical indicators
            return None
            
        try:
            # Parse candles into DataFrame
            df = pd.DataFrame(candles_list)
            
            # Map columns
            # Shoonya candle keys: into (open), inth (high), intl (low), intc (close), v (volume), time (timestamp)
            df['open'] = df['into'].astype(float)
            df['high'] = df['inth'].astype(float)
            df['low'] = df['intl'].astype(float)
            df['close'] = df['intc'].astype(float)
            df['volume'] = df['v'].astype(float)
            
            # Calculate SMA-20
            df['sma_20'] = df['close'].rolling(window=min(20, len(df))).mean()
            
            # Calculate EMA-50
            df['ema_50'] = df['close'].ewm(span=min(50, len(df)), adjust=False).mean()
            
            # Calculate RSI-14
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0.0)
            loss = -delta.where(delta < 0, 0.0)
            
            avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
            
            rs = avg_gain / (avg_loss + 1e-9) # Avoid division by zero
            df['rsi_14'] = 100 - (100 / (1 + rs))
            
            # Calculate MACD
            ema_12 = df['close'].ewm(span=12, adjust=False).mean()
            ema_26 = df['close'].ewm(span=26, adjust=False).mean()
            df['macd'] = ema_12 - ema_26
            df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
            df['macd_hist'] = df['macd'] - df['macd_signal']
            
            # Replace NaNs with None for JSON serialization
            df = df.replace({np.nan: None})
            
            return df
        except Exception as e:
            print(f"Error calculating indicators: {e}")
            return None

    def analyze_stock(self, quote, candles_list):
        """
        Runs AI technical analysis on a stock quote and candle history.
        Returns a structured recommendation dict.
        """
        symbol = quote.get("tsym", "Unknown")
        ltp = float(quote.get("lp", 0.0))
        pc = float(quote.get("pc", 0.0))
        cname = quote.get("cname", "Unknown Company")
        
        # 1. Calculate technical indicators
        df = self.calculate_indicators(candles_list)
        
        indicators_summary = {}
        recent_prices = []
        
        if df is not None:
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else latest
            
            indicators_summary = {
                "sma_20": latest["sma_20"],
                "ema_50": latest["ema_50"],
                "rsi_14": latest["rsi_14"],
                "macd": latest["macd"],
                "macd_signal": latest["macd_signal"],
                "macd_hist": latest["macd_hist"],
                "prev_macd_hist": prev["macd_hist"]
            }
            
            # Take last 10 candles for prompt
            for _, row in df.tail(10).iterrows():
                recent_prices.append({
                    "time": row["time"],
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row["volume"]
                })
        else:
            # Fallback when indicators cannot be calculated
            indicators_summary = {
                "sma_20": ltp,
                "ema_50": ltp,
                "rsi_14": 50.0,
                "macd": 0.0,
                "macd_signal": 0.0,
                "macd_hist": 0.0,
                "prev_macd_hist": 0.0
            }
            
            # Fallback simple prices
            for c in candles_list[-10:] if candles_list else []:
                recent_prices.append({
                    "time": c.get("time"),
                    "open": float(c.get("into", 0)),
                    "high": float(c.get("inth", 0)),
                    "low": float(c.get("intl", 0)),
                    "close": float(c.get("intc", 0)),
                    "volume": float(c.get("v", 0))
                })

        # 2. Check if we can run Gemini Analysis
        if self.model:
            try:
                # Construct Gemini Prompt
                prompt = f"""
                You are an elite automated quantitative trading agent. Analyze the following market data for stock {symbol} ({cname}) and make a trade recommendation (BUY, SELL, or HOLD).
                
                Current Market Quote:
                - Last Traded Price (LTP): {ltp}
                - Percentage Change today: {pc}%
                - Open: {quote.get('o')} | High: {quote.get('h')} | Low: {quote.get('l')} | Close: {quote.get('c')}
                - Today's Volume: {quote.get('v')}
                
                Calculated Technical Indicators:
                - SMA-20: {indicators_summary['sma_20']}
                - EMA-50: {indicators_summary['ema_50']}
                - RSI-14: {indicators_summary['rsi_14']}
                - MACD Line: {indicators_summary['macd']}
                - MACD Signal Line: {indicators_summary['macd_signal']}
                - MACD Histogram: {indicators_summary['macd_hist']} (Previous Period: {indicators_summary['prev_macd_hist']})
                
                Recent Candlestick Price History (last 10 periods):
                {json.dumps(recent_prices, indent=2)}
                
                Formulate a professional analysis based on:
                1. Trend direction (comparing LTP to EMA-50 and SMA-20).
                2. Momentum (inspecting RSI and MACD crossovers).
                3. Candlestick patterns (e.g. engulfing, doji, hammers) and volume support.
                4. Potential support and resistance levels.
                
                You MUST return your output in a single, strictly valid JSON format. Do NOT wrap the JSON inside markdown tags (like ```json). Return ONLY the raw JSON string. The keys of the JSON must be exactly:
                {{
                    "recommendation": "BUY" | "SELL" | "HOLD",
                    "confidence": <integer percentage from 0 to 100>,
                    "entry_price": <suggested buy entry price or current price, float>,
                    "target_price": <realistic profit target price, float>,
                    "stop_loss": <suggested stop loss price, float>,
                    "rationale": "<3-4 sentence detailed explanation of your indicators analysis, key levels, and risk factors>"
                }}
                """
                
                response = self.model.generate_content(prompt)
                res_text = response.text.strip()
                
                # Strip markdown JSON wrapping if model adds it
                if res_text.startswith("```json"):
                    res_text = res_text[7:]
                if res_text.endswith("```"):
                    res_text = res_text[:-3]
                res_text = res_text.strip()
                
                parsed_res = json.loads(res_text)
                return parsed_res
                
            except Exception as e:
                print(f"Gemini API analysis failed, falling back to rule-based: {e}")
                # Fallback to rule-based engine if api call fails

        # 3. Rule-Based Fallback Engine
        return self._rule_based_analysis(symbol, ltp, indicators_summary)

    def _rule_based_analysis(self, symbol, ltp, indicators):
        """
        Rule-based technical analysis engine. Used when Gemini API key is missing
        or request fails.
        """
        rsi = indicators.get("rsi_14", 50.0)
        macd = indicators.get("macd", 0.0)
        signal = indicators.get("macd_signal", 0.0)
        hist = indicators.get("macd_hist", 0.0)
        prev_hist = indicators.get("prev_macd_hist", 0.0)
        sma = indicators.get("sma_20", ltp)
        ema = indicators.get("ema_50", ltp)

        # Analysis logic
        recommendation = "HOLD"
        confidence = 50
        rationale_parts = []
        
        # Trend check
        if ltp > ema and ltp > sma:
            trend = "Bullish"
            rationale_parts.append(f"Price is in a strong uptrend above the 20-day SMA ({sma}) and 50-day EMA ({ema}).")
        elif ltp < ema and ltp < sma:
            trend = "Bearish"
            rationale_parts.append(f"Price is in a downtrend below the 20-day SMA ({sma}) and 50-day EMA ({ema}).")
        else:
            trend = "Sideways"
            rationale_parts.append("Price is consolidating near its moving averages, suggesting sideways momentum.")
            
        # Momentum checks
        momentum_score = 0
        if rsi < 30:
            momentum_score += 2
            rationale_parts.append(f"RSI ({rsi:.1f}) is oversold, indicating a high probability of a rebound.")
        elif rsi > 70:
            momentum_score -= 2
            rationale_parts.append(f"RSI ({rsi:.1f}) is overbought, signalling potential exhaustion and reversal.")
        else:
            rationale_parts.append(f"RSI ({rsi:.1f}) is in neutral territory.")
            
        # MACD checks
        if macd > signal and prev_hist <= 0 and hist > 0:
            momentum_score += 2
            rationale_parts.append("MACD shows a bullish crossover above the signal line.")
        elif macd < signal and prev_hist >= 0 and hist < 0:
            momentum_score -= 2
            rationale_parts.append("MACD shows a bearish crossover below the signal line.")
            
        # Synthesis
        if trend == "Bullish" and momentum_score >= 1:
            recommendation = "BUY"
            confidence = int(60 + (momentum_score * 10))
            entry_price = ltp
            target_price = round(ltp * 1.05, 2) # +5% target
            stop_loss = round(ltp * 0.97, 2)    # -3% stop loss
        elif trend == "Bearish" and momentum_score <= -1:
            recommendation = "SELL"
            confidence = int(60 + (abs(momentum_score) * 10))
            entry_price = ltp
            target_price = round(ltp * 0.95, 2) # -5% short target
            stop_loss = round(ltp * 1.03, 2)    # +3% stop loss
        else:
            recommendation = "HOLD"
            confidence = 50
            entry_price = ltp
            target_price = round(ltp * 1.02, 2)
            stop_loss = round(ltp * 0.98, 2)
            
        confidence = min(max(confidence, 10), 95) # Clamp between 10% and 95%
        
        rationale_parts.append(f"Based on technical rules, a {recommendation} position is recommended with {confidence}% confidence.")
        
        return {
            "recommendation": recommendation,
            "confidence": confidence,
            "entry_price": entry_price,
            "target_price": target_price,
            "stop_loss": stop_loss,
            "rationale": " ".join(rationale_parts)
        }
