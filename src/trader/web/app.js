'use strict';
const $ = id => document.getElementById(id);
const labels = {sma5m:'5 分鐘 SMA',trend1h:'1 小時趨勢',hold:'買入持有基準',cash:'全現金基準'};
const colors = {sma5m:'#80e7b1',trend1h:'#8cc5ff',hold:'#e6c48c',cash:'#94a7ad'};
const reasonLabels = {AI_ENTRY_DIRECTION_BLOCKED:'模型提出買入，但 1 小時趨勢未向上，已阻擋',AI_PREFILTER_DIRECTION_BLOCKED:'空倉且 1 小時趨勢未向上，本輪省略模型推論',AI_PREFILTER_DIRECTION_UNAVAILABLE:'1 小時趨勢資料不足或過期，本輪省略模型推論',AI_PREFILTER_COST_BLOCKED:'空倉且買入成本門檻未通過，本輪省略模型推論',AI_REFRESHED_QUOTE_STALE:'重新取得的報價仍已過期，放棄本次交易',AI_BUDGET_EXCEEDED:'模型建議金額超過可用下單預算',AI_SIGNAL_EXPIRED:'模型完成時原始訊號已過期，放棄本次交易',AI_PRICE_MOVED:'推論後價格變動超過 0.1%，放棄本次交易',AI_CONTEXT_CHANGED:'推論期間帳戶或行情上下文改變，放棄本次交易',AI_PROVIDER_UNAVAILABLE_OR_REFUSED:'模型連線失敗、逾時或未完整回覆；本輪改為觀望',AI_INVALID_PROPOSAL:'模型回覆格式不合規；本輪改為觀望',FEES_SLIPPAGE_AND_STRATEGY_EDGE:'成本／訊號門檻未通過',COOLDOWN:'交易冷卻期間',DAILY_TRADE_LIMIT:'當日成交次數上限',KILL_SWITCH_CLEAR:'交易已暫停',TOTAL_EXPOSURE_LIMIT:'總持倉上限',SYMBOL_ALLOCATION_LIMIT:'單幣配置上限',BALANCE_AND_NO_SHORTING:'可用資金或持幣不足',DAY_BASELINE_AVAILABLE:'缺少當日開盤基準',ORDER_NOTIONAL_LIMIT:'單筆額度上限',MARKET_DATA_FRESH:'行情已過期',EXCHANGE_NETWORK_ERROR:'行情連線失敗',EXCHANGE_RATE_LIMITED:'行情請求受限',KILL_SWITCH_ACTIVATED:'交易停止開關已開啟',KILL_SWITCH_RESUMED:'已恢復交易'};
const statusLabels = {FILLED:'模擬成交',HOLD:'等待訊號',REJECTED:'風控拒絕',ERROR:'行情錯誤',WARNING:'提醒',INFO:'資訊',CRITICAL:'重要事件'};
const money = value => '$'+Number(value).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const num = (value,d=2) => Number(value).toFixed(d);
const signed = value => (Number(value)>=0?'+':'−')+money(Math.abs(Number(value)));
const percent = value => (Number(value)>=0?'+':'−')+num(Math.abs(Number(value)))+'%';
const date = value => new Date(value).toLocaleString('zh-TW',{timeZone:'Asia/Taipei',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const element = (tag,text,cls) => {const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(cls)node.className=cls; return node;};
let page=1, pages=1, sequence=0;
async function api(path){const response=await fetch(path,{cache:'no-store',signal:AbortSignal.timeout(10000)});if(!response.ok)throw new Error('讀取失敗');return response.json();}
function alertMessage(message){$('alert').textContent=message;$('alert').hidden=!message;}
function cards(data){$('cards').replaceChildren();for(const [key,label] of Object.entries(labels)){const p=data.portfolios[key];const card=element('article',undefined,'card '+({sma5m:'sma',trend1h:'trend',hold:'hold',cash:'cash'}[key]));const top=element('div',undefined,'card-top');top.append(element('span',label),element('span',key==='hold'||key==='cash'?'基準':'策略','tag'));card.append(top,element('div',money(p.equity),'value'),element('p',signed(p.net_pnl)+'  /  '+percent(p.return_pct),'pnl '+(Number(p.net_pnl)>0?'positive':Number(p.net_pnl)<0?'negative':'neutral')));const bottom=element('div',undefined,'card-bottom');bottom.append(element('span','最大回落 '+num(p.drawdown_pct)+'%'),element('span','費用 '+money(p.fees)));card.append(bottom);card.append(element('p',p.stats?'成交 '+p.stats.fills+' 次 · 拒絕 '+p.stats.rejected+' 次':key==='hold'?data.risk.symbols.map(s=>s.replace('USDT','')+' 15%').join(' · ')+' · 現金 70%':'保持現金，沒有交易成本','card-detail'));$('cards').append(card);}}
function chart(points){const ns='http://www.w3.org/2000/svg';const svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 1100 260');svg.setAttribute('role','img');svg.setAttribute('aria-label','各策略美元損益曲線，完整數值請見上方策略卡片');function node(tag,attrs,text){const n=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));if(text)n.textContent=text;svg.append(n);return n;}
const values=points.flatMap(p=>Object.values(p.values).map(Number));let lo=Math.min(0,...values),hi=Math.max(0,...values);const pad=Math.max(.1,(hi-lo)*.15);lo-=pad;hi+=pad;const y=v=>220-(v-lo)/(hi-lo)*200;const times=points.map(p=>new Date(p.timestamp).getTime());const start=times[0],span=Math.max(1,times.at(-1)-start);const x=t=>70+(t-start)/span*1010;
for(let i=0;i<=4;i++){const value=lo+(hi-lo)*i/4;node('line',{x1:70,x2:1080,y1:y(value),y2:y(value),stroke:'#25343b','stroke-dasharray':'3 5'});node('text',{x:58,y:y(value)+4,fill:'#94a7ad','font-size':11,'text-anchor':'end'},(value<0?'−':'')+'$'+num(Math.abs(value)));}
for(const key of Object.keys(labels)){const coords=points.map((p,i)=>x(times[i])+','+y(Number(p.values[key]))).join(' ');node('polyline',{points:coords,fill:'none',stroke:colors[key],'stroke-width':key==='cash'?1:2,'stroke-dasharray':key==='cash'?'5 5':'none'});const last=points.at(-1);if(last)node('circle',{cx:x(times.at(-1)),cy:y(Number(last.values[key])),r:3,fill:colors[key]});}
if(points.length){node('text',{x:70,y:250,fill:'#94a7ad','font-size':11},date(points[0].timestamp));node('text',{x:1080,y:250,fill:'#94a7ad','font-size':11,'text-anchor':'end'},date(points.at(-1).timestamp));}$('chart').replaceChildren(svg);}
function renderAI(ai){$('ai-panel').hidden=!ai?.available;if(!ai?.available)return;
$('ai-info').textContent=ai.model+' · 呼叫 '+ai.calls+' 次 · 失敗 '+ai.failures+' 次 · 成交 '+ai.trades+' 次'+(ai.direction_filter_enabled?' · 進場方向實驗已啟用':ai.prefilter_enabled?' · 空倉成本預篩已啟用':'')+(ai.prefilter_skipped?' · 累計省略 '+ai.prefilter_skipped+' 次推論':'')+(ai.policy_version==='ai-budget-freshness-v2'?' · 預算／行情檢查 v2':'')+(ai.last_inference_seconds!=null?' · 最近推論 '+num(ai.last_inference_seconds,1)+' 秒':'');
$('ai-account').textContent='淨值 '+(ai.equity==null?'等待有效估值':money(ai.equity))+' · 現金 '+money(ai.cash)+' · 費用 '+money(ai.fees);
const age=ai.updated_at?(Date.now()-new Date(ai.updated_at).getTime())/60000:null;
$('ai-updated').textContent='開始 '+date(ai.started_at)+' · '+(ai.updated_at?'最後決策 '+date(ai.updated_at)+(age>15?'（超過 15 分鐘未更新）':''):'等待第一筆決策');}
function renderSummary(data){renderAI(data.ai);if(!data.ready){$('health').textContent='等待第一輪有效行情';$('cards').replaceChildren(element('p','帳本尚未產生觀測資料，請稍後再查看。','muted'));$('chart').replaceChildren();return;}
$('policy-version').textContent='出場規則版本 '+(data.risk.exit_policy_version||1)+' · '+data.risk.symbols.join(' / ');const stale=data.age_seconds>900;$('health').textContent=data.killed?'交易已暫停':stale?'資料更新延遲':data.killed===null?'控制狀態未知':'近期資料已更新';$('health-dot').classList.toggle('stale',stale||data.killed!==false);$('updated').textContent='最後觀測 '+date(data.updated_at);$('samples').textContent=data.samples+' 輪觀測';$('errors').textContent='行情錯誤 '+data.error_count+' 次';$('started').textContent='開始 '+date(data.started_at);$('capital').textContent=Number(data.capital).toLocaleString('en-US');cards(data);chart(data.points);if(stale)alertMessage('超過 15 分鐘沒有新觀測。請檢查 Docker、網路或電腦是否進入睡眠；下方仍是最後保存的資料。');}
function renderRecords(data){pages=data.pages;$('records').replaceChildren();$('record-count').textContent=data.total+' 筆';$('page-info').textContent='第 '+page+' / '+pages+' 頁 · 每頁 30 筆';$('prev').disabled=page<=1;$('next').disabled=page>=pages;if(!data.rows.length){const row=element('tr');const cell=element('td','目前沒有符合條件的紀錄。','empty');cell.colSpan=5;row.append(cell);$('records').append(row);return;}
for(const r of data.rows){const tr=element('tr');tr.append(element('td',date(r.timestamp)),element('td',r.symbol||'—'),element('td',({BUY:'買入',SELL:'賣出',HOLD:'觀望'})[r.action]||'—'));const state=element('td');state.append(element('span',statusLabels[r.status]||r.status,'badge '+r.status));tr.append(state);let text=r.reason?(reasonLabels[r.reason]||r.reason):r.status==='HOLD'?'未提出買賣，等待下一次策略評估':(r.reasons||[]).map(x=>reasonLabels[x]||x).join('、');if(r.order)text='成交 '+num(r.order.executed_qty,8)+' · 價格 '+money(r.order.average_fill_price)+' · 費用 '+money(r.order.fee);const detail=element('td',text);if(r.model_reason)detail.append(element('div','模型／系統說明：'+(reasonLabels[r.model_reason]||r.model_reason),'detail'));if(r.model)detail.append(element('div',r.model,'detail'));if(r.status==='REJECTED'&&r.signal!=null&&r.cost!=null)detail.append(element('div','訊號代理值 '+num(r.signal)+' bps / 來回成本 '+num(r.cost)+' bps','detail'));tr.append(detail);$('records').append(tr);}}

const rendered = new Map();
function changed(key, data, render) {
  const fingerprint=JSON.stringify(data);
  if(rendered.get(key)!==fingerprint){render(data);rendered.set(key,fingerprint);}
}
function renderMarkets(data){
  $('market-prices').replaceChildren();
  for(const symbol of data.symbols||Object.keys(data.markets||{})){
    const quote=data.markets?.[symbol];
    $('market-prices').append(element('p',symbol+' · '+(quote?money(quote.price)+' USDT · '+date(quote.timestamp):'等待下一輪行情')));
  }
  $('holdings').replaceChildren();
  for(const account of data.holdings||[]){
    const box=element('div',undefined,'holding-account');
    box.append(element('h3',labels[account.strategy]||'Ollama AI'),element('p','現金 '+money(account.cash)+' · 持倉市值 '+money(Number(account.equity)-Number(account.cash))+' · 估值 '+date(account.timestamp),'muted'));
    if(!account.positions.length)box.append(element('p','尚未持有幣，資金為現金。','muted'));
    for(const p of account.positions)box.append(element('p',p.symbol+' · 數量 '+num(p.quantity,8)+' · 估值價格 '+money(p.market_price)+' · 市值 '+money(p.market_value)));
    $('holdings').append(box);
  }
}
async function sync(){
  const seq=++sequence;
  const source=$('source').value,status=$('status').value;
  const results=await Promise.allSettled([api('/api/summary'),api('/api/records?'+new URLSearchParams({source,status,page:String(page)})),api('/api/readiness')]);
  if(seq!==sequence)return;
  alertMessage('');
  if(results[2].status==='fulfilled')changed('readiness',results[2].value,renderReadiness);
  else {$('launch-status').textContent='驗收資料暫時無法讀取；實盤維持關閉';$('launch-approve').disabled=true;}
  if(results[0].status==='fulfilled'){
    const data=results[0].value;
    changed('markets',{markets:data.markets,holdings:data.holdings,symbols:data.risk?.symbols},renderMarkets);
    // Exclude the ticking age from the content fingerprint, but preserve stale transitions.
    const stable={...data,age_seconds:data.age_seconds>900?901:0};
    changed('summary',stable,renderSummary);
    if(data.age_seconds>900)alertMessage('超過 15 分鐘沒有新觀測；目前顯示最後保存的資料。');
  }else{
    rendered.delete('summary');
    $('health').textContent='連線中斷，正在自動重試';
    $('health-dot').classList.add('stale');
    alertMessage('暫時無法取得資料，保留最後內容並自動重試。');
  }
  if(results[1].status==='fulfilled')changed('records',results[1].value,renderRecords);
  else alertMessage('紀錄暫時無法讀取，保留最後內容並自動重試。');
}
$('source').addEventListener('change',()=>{page=1;rendered.delete('records');const events=['errors','events','ai-events','ai-calls'].includes($('source').value);$('status').disabled=events;if(events)$('status').value='ALL';sync();});
$('status').addEventListener('change',()=>{page=1;rendered.delete('records');sync();});
$('prev').addEventListener('click',()=>{if(page>1){page--;sync();}});
$('next').addEventListener('click',()=>{if(page<pages){page++;sync();}});
async function monitor(){if(!document.hidden)await sync();setTimeout(monitor,5000);}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)sync();});
monitor();

$('export-form').addEventListener('submit',event=>{
 const a=$('export-start').value,b=$('export-end').value;
 if(a&&b&&a>=b){event.preventDefault();alertMessage('匯出開始時間必須早於結束時間。');return;}
 $('export-start-utc').value=a?a+':00+08:00':'';
 $('export-end-utc').value=b?b+':00+08:00':'';
});

function renderReadiness(data){
  $('launch-checks').replaceChildren();$('launch-blockers').replaceChildren();
  if(!data.available){$('launch-status').textContent='尚未連接 Testnet 驗收資料；實盤未啟用';$('launch-policy').textContent='';$('launch-progress').textContent='';return;}
  const p=data.policy,o=data.observation;
  $('launch-status').textContent='實盤未啟用 · '+({RUNNING:'測試網觀察進行中',BLOCKED:'測試已停機，需檢查',OBSERVATION_COMPLETE_REVIEW_REQUIRED:'觀察結束，等待驗收'}[o.status]||'等待驗收');
  $('launch-policy').textContent=`本金 ${p.capital} USDT · 單筆 ${p.order_limit} · 總持倉 ${p.exposure_limit} · 每日虧損 ${p.daily_loss}／累計 ${p.cumulative_loss} 停買 · 不自動加碼`;
  $('launch-progress').textContent=`${o.cycles} 輪觀察 · ${o.orders} 筆成交 · 淨值 ${o.equity||'—'} USDT · 最後更新 ${o.last_cycle?date(o.last_cycle):'—'} · 觀察期結束 ${o.deadline?date(o.deadline*1000):'—'}`;
  const labels={current_policy:'已套用同意的限額','48h_observed':'完成 48 小時觀察','500_cycles':'至少 500 輪有效觀察',max_gap_15m:'最大觀察間隔不超過 15 分鐘',strategy_buy_and_sell:'策略買入與賣出均已成交',no_pending_order:'沒有待確認訂單',no_persistent_stop:'未觸發停機',observation_completed:'觀察期已結束',no_faults:'沒有異常停機紀錄'};
  for(const [key,ok] of Object.entries(data.testnet_checks))$('launch-checks').append(element('li',(ok?'✓ 已符合：':'○ 未符合：')+(labels[key]||key)));
  const blockers={PRODUCTION_KEY_PERMISSIONS_NOT_VERIFIED:'正式 API 權限尚未驗證',AUTHENTICATED_PER_ORDER_APPROVAL_REQUIRED:'正式入口已建置，仍須帳戶檢查與逐筆人工放行',STRATEGY_PERFORMANCE_REVIEW_REQUIRED:'策略表現尚待人工評估'};
  for(const key of data.live_blockers)$('launch-blockers').append(element('li',blockers[key]||key));
}

let liveProposal=null,liveBusy=false;
const liveErrors={PRODUCTION_CREDENTIALS_MISSING:'尚未設定正式 API 金鑰',TESTNET_ACCEPTANCE_NOT_PASSED:'測試網驗收尚未通過',LIVE_EXECUTION_DISABLED:'伺服器正式下單開關仍關閉',AUTH_REQUIRED:'請輸入正確的操作密碼',UNSAFE_OR_MISSING_KEY_PERMISSIONS:'金鑰權限不符合要求或缺少欄位，請檢查 IP 限制、現貨交易及提款／轉帳權限',PENDING_ORDER_RECONCILE_REQUIRED:'有待確認訂單，請先查詢成交',PERSISTENT_STOP:'正式下單已停止',NO_PENDING_ORDER:'目前沒有待確認訂單',PROPOSAL_EXPIRED_OR_KEY_CHANGED:'交易預覽已過期，請重新準備',ACCOUNT_DRIFT_RECONCILE_REQUIRED:'實際餘額與本系統帳本不一致，請核對',CONTROL_UNAVAILABLE_CHECK_STATUS:'控制服務暫時無回應；請先查詢狀態與成交，勿重複送單',LIVE_CONTROL_NOT_CONFIGURED:'此頁尚未連接正式控制服務',RATE_OR_COOLDOWN_LIMIT:'尚在 30 分鐘冷卻期，或已達每日 4 筆上限'};
async function liveStatus(){
 try{const r=await fetch('/api/live/status');const d=await r.json();if(!r.ok)throw Error(d.error);$('live-status').textContent=`正式金鑰：${d.credentials_configured?'已設定（仍需檢查）':'未設定'} · 下單開關：${d.execution_enabled?'已開':'關閉'} · 測試網驗收：${d.testnet_passed?'符合':'未通過'} · 停機：${d.stop_active?'是':'否'} · 正式成交 ${d.state.orders||0} 筆 · 現金 ${d.state.cash||'—'} USDT · BTC ${d.state.btc||'0'} · 狀態 ${d.state.status} · 上次權限與餘額檢查 ${d.state.last_check?date(d.state.last_check*1000):'尚未通過'}`;}
 catch(e){$('live-status').textContent=liveErrors[e.message]||'正式控制服務尚未就緒';}
}
async function liveAction(action,body={}){
 if(location.protocol!=='https:'&&!['localhost','127.0.0.1','[::1]'].includes(location.hostname)){$('live-result').textContent='正式操作請使用 SSH 加密連線，再開啟 http://localhost:18080；操作密碼不會送出。';return;}
 if(liveBusy&&action!=='stop')return;liveBusy=true;$('live-approve').disabled=true;
 $('live-result').textContent='處理中，請等待確認…';
 try{const r=await fetch('/api/live/'+action,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+$('live-token').value},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error);
 if(action==='prepare'){liveProposal=d;$('live-preview').textContent=`真實交易預覽：${d.side==='BUY'?'買入':'賣出'} ${d.quantity} BTC，預估 ${Number(d.estimated_usdt).toFixed(4)} USDT（費用另計），${date(d.expires*1000)} 到期。`;}
 else if(action==='approve'||action==='stop'||action==='resume'){liveProposal=null;$('live-preview').textContent='沒有可放行的交易預覽';$('live-confirm').value='';$('live-reviewed').checked=false;}
 $('live-result').textContent=action==='approve'?'已送出並確認成交；餘額已更新。':'操作完成。';}
 catch(e){$('live-result').textContent=liveErrors[e.message]||e.message;if(action==='approve'){liveProposal=null;$('live-preview').textContent='放行未完成或結果待確認，請先查詢成交與狀態。';}}
 finally{liveBusy=false;liveEnable();await liveStatus();}
}
function liveEnable(){$('live-approve').disabled=liveBusy||!liveProposal||Date.now()>liveProposal.expires*1000||!$('live-reviewed').checked||$('live-confirm').value!=='我確認使用真實 USDT 下這一筆訂單';}
$('live-preflight').addEventListener('click',()=>liveAction('preflight'));
$('live-reconcile').addEventListener('click',()=>liveAction('reconcile'));
$('live-stop').addEventListener('click',()=>liveAction('stop'));
$('live-resume').addEventListener('click',()=>{const text=prompt('核對帳本後，輸入：我確認已核對帳本並解除停機');if(text)liveAction('resume',{confirmation:text});});
$('live-prepare').addEventListener('click',()=>{liveProposal=null;liveAction('prepare',{side:$('live-side').value,amount:$('live-amount').value});});
$('live-approve').addEventListener('click',()=>{if(liveProposal)liveAction('approve',{id:liveProposal.id,confirmation:$('live-confirm').value,reviewed:$('live-reviewed').checked});});
$('live-confirm').addEventListener('input',liveEnable);$('live-reviewed').addEventListener('change',liveEnable);
setInterval(()=>{liveEnable();if(!liveBusy)liveStatus();},10000);liveStatus();
