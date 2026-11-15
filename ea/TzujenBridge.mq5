//+------------------------------------------------------------------+
//| TzujenBridge.mq5                                                  |
//| 只負責：同步 K 棒／持倉／掛單／成交給策略服務，執行服務回傳的動作      |
//| 策略邏輯全部在伺服器（cfd-live），這裡不做任何判斷                   |
//+------------------------------------------------------------------+
#property copyright "tzujenlab"
#property version   "1.01"

#include <Trade\Trade.mqh>

input string ServerURL    = "https://cfd.tzujenlab.com";  // 策略服務網址（要加到 工具→選項→EA交易→允許 WebRequest）
input string Token        = "";                            // 與服務的 EA_TOKEN 相同
input int    SyncMs       = 2000;                          // 同步間隔（毫秒）
input bool   AllowLive    = false;                         // 允許在真實帳戶下單（預設只在模擬帳戶下單）
input int    MaxBarsPerSync = 3000;                        // 每次最多送幾根 1 分 K

#define EA_VERSION "1.01"

CTrade   trade;
string   g_syms[];
long     g_since[];
ulong    g_last_deal = 0;
bool     g_first = true;
string   g_results = "";      // 上次動作的結果（JSON 片段，逗號分隔）
long     g_done[];            // 已執行過的動作 id（避免重複執行）
int      g_fail = 0;
bool     g_kill = false;

//+------------------------------------------------------------------+
int OnInit()
  {
   if(StringLen(Token) < 8)
     {
      Alert("TzujenBridge：請在參數填入 Token（與服務的 EA_TOKEN 相同）");
      return(INIT_PARAMETERS_INCORRECT);
     }
   trade.SetDeviationInPoints(100);
   trade.SetAsyncMode(false);
   EventSetMillisecondTimer(MathMax(SyncMs, 500));
   Print("TzujenBridge ", EA_VERSION, " 啟動，伺服器 ", ServerURL);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason) { EventKillTimer(); }

//+------------------------------------------------------------------+
string Esc(string s)
  {
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   StringReplace(s, "\n", " ");
   StringReplace(s, "\r", " ");
   return s;
  }

string D(double v, int digits = 6) { return DoubleToString(v, digits); }

bool IsDone(long id)
  {
   for(int i = 0; i < ArraySize(g_done); i++) if(g_done[i] == id) return true;
   return false;
  }

void MarkDone(long id)
  {
   int n = ArraySize(g_done);
   if(n >= 500) { ArrayRemove(g_done, 0, 100); n = ArraySize(g_done); }
   ArrayResize(g_done, n + 1);
   g_done[n] = id;
  }

void AddResult(long id, bool ok, uint code, ulong order, ulong deal, string msg)
  {
   string r = "{\"id\":" + IntegerToString(id) + ",\"ok\":" + (ok ? "true" : "false") +
              ",\"retcode\":" + IntegerToString(code) + ",\"order\":" + IntegerToString((long)order) +
              ",\"deal\":" + IntegerToString((long)deal) + ",\"msg\":\"" + Esc(msg) + "\"}";
   g_results += (StringLen(g_results) > 0 ? "," : "") + r;
  }

//+------------------------------------------------------------------+
string JsonPositions()
  {
   string s = "";
   for(int i = 0; i < PositionsTotal(); i++)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || !PositionSelectByTicket(t)) continue;
      string one = "{\"ticket\":" + IntegerToString((long)t) +
                   ",\"symbol\":\"" + Esc(PositionGetString(POSITION_SYMBOL)) + "\"" +
                   ",\"type\":" + IntegerToString(PositionGetInteger(POSITION_TYPE)) +
                   ",\"volume\":" + D(PositionGetDouble(POSITION_VOLUME), 2) +
                   ",\"price\":" + D(PositionGetDouble(POSITION_PRICE_OPEN)) +
                   ",\"sl\":" + D(PositionGetDouble(POSITION_SL)) +
                   ",\"tp\":" + D(PositionGetDouble(POSITION_TP)) +
                   ",\"magic\":" + IntegerToString(PositionGetInteger(POSITION_MAGIC)) +
                   ",\"time\":" + IntegerToString(PositionGetInteger(POSITION_TIME)) +
                   ",\"profit\":" + D(PositionGetDouble(POSITION_PROFIT), 2) +
                   ",\"swap\":" + D(PositionGetDouble(POSITION_SWAP), 2) +
                   ",\"comment\":\"" + Esc(PositionGetString(POSITION_COMMENT)) + "\"}";
      s += (StringLen(s) > 0 ? "," : "") + one;
     }
   return "[" + s + "]";
  }

string OrderTypeName(long t)
  {
   switch((int)t)
     {
      case ORDER_TYPE_BUY_STOP:  return "BUY_STOP";
      case ORDER_TYPE_SELL_STOP: return "SELL_STOP";
      case ORDER_TYPE_BUY_LIMIT: return "BUY_LIMIT";
      case ORDER_TYPE_SELL_LIMIT:return "SELL_LIMIT";
     }
   return IntegerToString(t);
  }

string JsonOrders()
  {
   string s = "";
   for(int i = 0; i < OrdersTotal(); i++)
     {
      ulong t = OrderGetTicket(i);
      if(t == 0 || !OrderSelect(t)) continue;
      string one = "{\"ticket\":" + IntegerToString((long)t) +
                   ",\"symbol\":\"" + Esc(OrderGetString(ORDER_SYMBOL)) + "\"" +
                   ",\"type\":\"" + OrderTypeName(OrderGetInteger(ORDER_TYPE)) + "\"" +
                   ",\"volume\":" + D(OrderGetDouble(ORDER_VOLUME_CURRENT), 2) +
                   ",\"price\":" + D(OrderGetDouble(ORDER_PRICE_OPEN)) +
                   ",\"sl\":" + D(OrderGetDouble(ORDER_SL)) +
                   ",\"tp\":" + D(OrderGetDouble(ORDER_TP)) +
                   ",\"magic\":" + IntegerToString(OrderGetInteger(ORDER_MAGIC)) +
                   ",\"time\":" + IntegerToString(OrderGetInteger(ORDER_TIME_SETUP)) +
                   ",\"comment\":\"" + Esc(OrderGetString(ORDER_COMMENT)) + "\"}";
      s += (StringLen(s) > 0 ? "," : "") + one;
     }
   return "[" + s + "]";
  }

string JsonDeals()
  {
   datetime from = g_first ? TimeCurrent() - 7 * 86400 : TimeCurrent() - 3 * 86400;
   if(!HistorySelect(from, TimeCurrent() + 86400)) return "[]";
   string s = "";
   ulong maxt = g_last_deal;
   int n = 0;
   for(int i = 0; i < HistoryDealsTotal() && n < 300; i++)
     {
      ulong t = HistoryDealGetTicket(i);
      if(t == 0 || t <= g_last_deal) continue;
      long typ = HistoryDealGetInteger(t, DEAL_TYPE);
      if(typ != DEAL_TYPE_BUY && typ != DEAL_TYPE_SELL) continue;
      string one = "{\"ticket\":" + IntegerToString((long)t) +
                   ",\"order\":" + IntegerToString(HistoryDealGetInteger(t, DEAL_ORDER)) +
                   ",\"position\":" + IntegerToString(HistoryDealGetInteger(t, DEAL_POSITION_ID)) +
                   ",\"symbol\":\"" + Esc(HistoryDealGetString(t, DEAL_SYMBOL)) + "\"" +
                   ",\"type\":" + IntegerToString(typ) +
                   ",\"entry\":" + IntegerToString(HistoryDealGetInteger(t, DEAL_ENTRY)) +
                   ",\"volume\":" + D(HistoryDealGetDouble(t, DEAL_VOLUME), 2) +
                   ",\"price\":" + D(HistoryDealGetDouble(t, DEAL_PRICE)) +
                   ",\"profit\":" + D(HistoryDealGetDouble(t, DEAL_PROFIT), 2) +
                   ",\"swap\":" + D(HistoryDealGetDouble(t, DEAL_SWAP), 2) +
                   ",\"commission\":" + D(HistoryDealGetDouble(t, DEAL_COMMISSION), 2) +
                   ",\"magic\":" + IntegerToString(HistoryDealGetInteger(t, DEAL_MAGIC)) +
                   ",\"time\":" + IntegerToString(HistoryDealGetInteger(t, DEAL_TIME)) +
                   ",\"comment\":\"" + Esc(HistoryDealGetString(t, DEAL_COMMENT)) + "\"}";
      s += (StringLen(s) > 0 ? "," : "") + one;
      if(t > maxt) maxt = t;
      n++;
     }
   g_last_deal = maxt;
   return "[" + s + "]";
  }

string JsonBars()
  {
   string s = "";
   for(int k = 0; k < ArraySize(g_syms); k++)
     {
      string sym = g_syms[k];
      SymbolSelect(sym, true);
      MqlRates r[];
      datetime cur = iTime(sym, PERIOD_M1, 0);          // 正在形成的那根，不送
      if(cur == 0) continue;
      int got = CopyRates(sym, PERIOD_M1, (datetime)(g_since[k] + 60), cur - 60, r);
      string rows = "";
      int n = 0;
      for(int i = 0; i < got && n < MaxBarsPerSync; i++)
        {
         if(r[i].time >= cur) break;
         rows += (n > 0 ? "," : "") + "[" + IntegerToString((long)r[i].time) + "," + D(r[i].open) + "," + D(r[i].high) + "," +
                 D(r[i].low) + "," + D(r[i].close) + "," + IntegerToString((long)r[i].tick_volume) + "]";
         n++;
        }
      s += (StringLen(s) > 0 ? "," : "") + "\"" + Esc(sym) + "\":[" + rows + "]";
     }
   return "{" + s + "}";
  }

string JsonTicks()
  {
   string s = "";
   for(int k = 0; k < ArraySize(g_syms); k++)
     {
      MqlTick t;
      if(!SymbolInfoTick(g_syms[k], t)) continue;
      s += (StringLen(s) > 0 ? "," : "") + "\"" + Esc(g_syms[k]) + "\":{\"bid\":" + D(t.bid) + ",\"ask\":" + D(t.ask) +
           ",\"time\":" + IntegerToString((long)t.time) +
           ",\"last_m1\":" + IntegerToString((long)iTime(g_syms[k], PERIOD_M1, 1)) + "}";   // 最新一根已完成的 1 分 K
     }
   return "{" + s + "}";
  }

//+------------------------------------------------------------------+
double NormPrice(string sym, double p)
  {
   if(p <= 0) return 0.0;
   double ts = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   int dg = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   if(ts > 0) p = MathRound(p / ts) * ts;
   return NormalizeDouble(p, dg);
  }

double NormVol(string sym, double v)
  {
   double step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double mn = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   if(step > 0) v = MathRound(v / step) * step;
   if(v < mn) v = mn;
   return NormalizeDouble(v, 2);
  }

void Exec(string line)
  {
   string f[];
   int n = StringSplit(line, '|', f);
   if(n < 12) return;
   long   id     = StringToInteger(f[1]);
   string op     = f[2];
   string sym    = f[3];
   long   magic  = StringToInteger(f[4]);
   string side   = f[5];
   double vol    = StringToDouble(f[6]);
   double price  = StringToDouble(f[7]);
   double sl     = StringToDouble(f[8]);
   double tp     = StringToDouble(f[9]);
   ulong  ticket = (ulong)StringToInteger(f[10]);
   string cmt    = f[11];
   if(IsDone(id)) return;
   MarkDone(id);

   bool isLive = AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL;
   if(isLive && !AllowLive && (op == "MARKET" || op == "PENDING"))
     {
      AddResult(id, false, 0, 0, 0, "真實帳戶未允許下單（EA 參數 AllowLive=false）");
      return;
     }
   SymbolSelect(sym, true);
   trade.SetExpertMagicNumber((ulong)magic);
   trade.SetTypeFillingBySymbol(sym);
   price = NormPrice(sym, price); sl = NormPrice(sym, sl); tp = NormPrice(sym, tp);
   bool ok = false;
   if(op == "MARKET")
     {
      vol = NormVol(sym, vol);
      ok = (side == "BUY") ? trade.Buy(vol, sym, 0.0, sl, tp, cmt) : trade.Sell(vol, sym, 0.0, sl, tp, cmt);
     }
   else if(op == "PENDING")
     {
      vol = NormVol(sym, vol);
      if(side == "BUY_STOP")       ok = trade.BuyStop(vol, price, sym, sl, tp, ORDER_TIME_GTC, 0, cmt);
      else if(side == "SELL_STOP") ok = trade.SellStop(vol, price, sym, sl, tp, ORDER_TIME_GTC, 0, cmt);
      else if(side == "BUY_LIMIT") ok = trade.BuyLimit(vol, price, sym, sl, tp, ORDER_TIME_GTC, 0, cmt);
      else if(side == "SELL_LIMIT")ok = trade.SellLimit(vol, price, sym, sl, tp, ORDER_TIME_GTC, 0, cmt);
     }
   else if(op == "CANCEL") ok = trade.OrderDelete(ticket);
   else if(op == "MODIFY") ok = trade.PositionModify(ticket, sl, tp);
   else if(op == "CLOSE")  ok = trade.PositionClose(ticket);
   else
     {
      AddResult(id, false, 0, 0, 0, "未知動作 " + op);
      return;
     }
   uint code = trade.ResultRetcode();
   bool good = (code == TRADE_RETCODE_DONE || code == TRADE_RETCODE_PLACED || code == TRADE_RETCODE_DONE_PARTIAL);
   AddResult(id, ok && good, code, trade.ResultOrder(), trade.ResultDeal(), trade.ResultRetcodeDescription());
   Print("TzujenBridge 動作 #", id, " ", op, " ", sym, " ", side, " → ", code, " ", trade.ResultRetcodeDescription());
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   string body = "{\"ea_version\":\"" + EA_VERSION + "\"" +
                 ",\"login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) +
                 ",\"server\":\"" + Esc(AccountInfoString(ACCOUNT_SERVER)) + "\"" +
                 ",\"company\":\"" + Esc(AccountInfoString(ACCOUNT_COMPANY)) + "\"" +
                 ",\"currency\":\"" + Esc(AccountInfoString(ACCOUNT_CURRENCY)) + "\"" +
                 ",\"trade_mode\":" + IntegerToString(AccountInfoInteger(ACCOUNT_TRADE_MODE)) +
                 ",\"connected\":" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false") +
                 ",\"trade_allowed\":" + (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && MQLInfoInteger(MQL_TRADE_ALLOWED) ? "true" : "false") +
                 ",\"balance\":" + D(AccountInfoDouble(ACCOUNT_BALANCE), 2) +
                 ",\"equity\":" + D(AccountInfoDouble(ACCOUNT_EQUITY), 2) +
                 ",\"margin_free\":" + D(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) +
                 ",\"time\":" + IntegerToString((long)TimeCurrent()) +
                 ",\"positions\":" + JsonPositions() +
                 ",\"orders\":" + JsonOrders() +
                 ",\"deals\":" + JsonDeals() +
                 ",\"bars\":" + JsonBars() +
                 ",\"ticks\":" + JsonTicks() +
                 ",\"results\":[" + g_results + "]}";

   char data[], res[];
   string resHeaders;
   int len = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(len > 0) ArrayResize(data, len - 1);                 // 去掉結尾的 \0
   string headers = "Content-Type: application/json\r\nX-Token: " + Token + "\r\n";
   ResetLastError();
   int code = WebRequest("POST", ServerURL + "/ea/sync", headers, 8000, data, res, resHeaders);
   if(code != 200)
     {
      g_fail++;
      int err = GetLastError();
      if(g_fail == 1 || g_fail % 30 == 0)
        {
         if(err == 4014) Print("TzujenBridge：網址未被允許。請到 工具→選項→EA交易，勾選允許 WebRequest 並加入 ", ServerURL);
         else Print("TzujenBridge：同步失敗 HTTP ", code, " err ", err, "（連續 ", g_fail, " 次）");
        }
      return;                                              // 結果保留到下次再送
     }
   if(g_fail > 0) Print("TzujenBridge：恢復連線");
   g_fail = 0;
   g_first = false;
   g_results = "";

   string text = CharArrayToString(res, 0, WHOLE_ARRAY, CP_UTF8);
   string lines[];
   int nl = StringSplit(text, '\n', lines);
   ArrayResize(g_syms, 0); ArrayResize(g_since, 0);
   for(int i = 0; i < nl; i++)
     {
      string ln = lines[i];
      StringTrimRight(ln); StringTrimLeft(ln);
      if(StringLen(ln) < 2) continue;
      if(StringSubstr(ln, 0, 2) == "S|")
        {
         string f[];
         if(StringSplit(ln, '|', f) >= 3)
           {
            int k = ArraySize(g_syms);
            ArrayResize(g_syms, k + 1); ArrayResize(g_since, k + 1);
            g_syms[k] = f[1]; g_since[k] = StringToInteger(f[2]);
           }
        }
      else if(StringSubstr(ln, 0, 3) == "OK|")
        {
         string f[];
         if(StringSplit(ln, '|', f) >= 3) g_kill = (f[2] == "1");
        }
     }
   for(int i = 0; i < nl; i++)
     {
      string ln = lines[i];
      StringTrimRight(ln); StringTrimLeft(ln);
      if(StringSubstr(ln, 0, 2) == "A|") Exec(ln);
     }
  }
//+------------------------------------------------------------------+
