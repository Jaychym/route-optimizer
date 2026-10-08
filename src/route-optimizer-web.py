#!/usr/bin/env python3
"""Read-only, standard-library-only Route Optimizer v3.3 monitoring server."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA_DIR = Path('/var/lib/route-optimizer')
FILES = {
    '/api/status': DATA_DIR / 'dashboard-v3.json',
    '/api/history': DATA_DIR / 'quality-history-v3.json',
    '/api/route-events': DATA_DIR / 'route-events-v3.json',
}
MAX_JSON_BYTES = 2_000_000

HTML = r'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><title>Route Optimizer • Network Control</title>
<style>
:root{color-scheme:dark;--bg:#080d15;--surface:#111926;--surface2:#151f2e;--border:#283448;--text:#e6edf7;--muted:#92a2b8;--frontier:#57b9fa;--spectrum:#ffc17b;--green:#64dbb6;--red:#f78c92;--orange:#ffca81}
*{box-sizing:border-box}html{font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}body{background:var(--bg);color:var(--text);margin:0;font-size:13px}h1,h2,h3,p{margin-top:0}h1{font-size:23px;letter-spacing:-.6px;margin:0 0 4px}h2{font-size:15px;margin:0}h3{font-size:13px;margin:0}small,.muted{color:var(--muted)}header{background:#0e1420;border-bottom:1px solid var(--border);padding:24px 0}main,.wrap{max-width:1600px;padding:0 26px;margin:auto}header .wrap{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}.subheading{font-size:12px;color:var(--muted)}.status-line{display:flex;gap:9px;align-items:center;font-size:11px;text-transform:uppercase;letter-spacing:.7px;font-weight:700;color:#becbdc}.dot{width:9px;height:9px;background:var(--red);border-radius:50%;display:inline-block}.dot.ok{background:var(--green)}.dot.dry{background:var(--orange)}main{padding-top:23px}#notice{min-height:20px;color:var(--orange);margin-bottom:12px}.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:16px}.kpi,.panel{background:var(--surface);border:1px solid var(--border);border-radius:10px}.kpi{padding:14px}.kpi span{font-size:11px;color:var(--muted)}.kpi strong{font-size:25px;display:block;margin-top:5px;letter-spacing:-.7px}.panel{overflow:hidden;margin-bottom:15px}.panelhead{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:16px 16px 14px;border-bottom:1px solid var(--border)}.panelhead p{margin:4px 0 0;color:var(--muted);font-size:11px}.buttons{display:flex;gap:8px;flex-wrap:wrap}.buttons input,.buttons select,.buttons button{border:1px solid #39465a;background:#0a121d;color:var(--text);border-radius:7px;padding:8px 9px;font:inherit}.buttons input{min-width:185px}.buttons button{cursor:pointer}.buttons button:hover{border-color:var(--frontier)}.healthgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:0 0 15px}.healthbox{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:13px 14px}.healthbox label{display:block;color:var(--muted);font-size:11px;margin-bottom:8px}.healthbox strong{display:block;font-size:18px;margin-bottom:4px}.healthbox small{display:block;font-size:11px}.metric-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.metric-card{padding:12px 14px 15px;min-width:0}.legend{display:flex;gap:13px;flex-wrap:wrap;color:var(--muted);font-size:11px;margin:8px 0 0}.legend b{font-weight:500}.swatch{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px}.swatch.fr{background:var(--frontier)}.swatch.sp{background:var(--spectrum)}.chart-area{height:174px;margin-top:10px}.chart-area svg{width:100%;height:100%;display:block}.chart-footer{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:3px;gap:6px}.summary{display:grid;grid-template-columns:1.1fr 1fr;gap:14px;padding:14px 16px}.route-line{display:flex;justify-content:space-between;align-items:center;margin:6px 0;color:var(--muted)}.bar{display:flex;border-radius:12px;background:#273146;overflow:hidden;height:12px;margin:10px 0}.bar>div{transition:width .3s}.bar .fr{background:var(--frontier)}.bar .sp{background:var(--spectrum)}.route-caption{font-size:11px;color:var(--muted)}.scroll{overflow:auto;max-height:650px}.scroll thead{position:sticky;top:0;background:#131c29;z-index:1}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:10px 11px;border-bottom:1px solid var(--border);text-align:left;white-space:nowrap}th{font-weight:650;color:var(--muted);text-transform:uppercase;font-size:10px;letter-spacing:.5px}th.num,td.num{text-align:right}tbody tr:hover{background:#192538}code,.mono{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}code{color:#d1e3ff}.good{color:var(--green)}.warn{color:var(--orange)}.bad{color:var(--red)}.info{color:var(--frontier)}.empty{padding:16px;color:var(--muted)}.two{display:grid;grid-template-columns:1fr 1fr;gap:15px}.timeline{max-height:350px;overflow:auto}.event{padding:11px 15px;border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:5px}.event .detail{color:var(--muted);font-size:11px;overflow-wrap:anywhere}.event strong{font-weight:620}.event time{font-size:11px;color:var(--muted)}footer{padding:12px 0 32px;color:var(--muted);font-size:11px}.right{display:flex;justify-content:flex-end}#history-period{min-width:88px}a{color:var(--frontier)}
@media(max-width:1150px){.kpis{grid-template-columns:repeat(3,1fr)}.metric-grid{grid-template-columns:1fr}.healthgrid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:750px){main,.wrap{padding-left:14px;padding-right:14px}.two,.summary{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(2,1fr)}.kpi strong{font-size:21px}.healthgrid{grid-template-columns:1fr 1fr}}
</style></head><body>
<header><div class="wrap"><div><h1>Route Optimizer</h1><div class="subheading">UniFi gateway · dual WAN · IPFIX / BGP</div></div><div class="status-line"><span id="led" class="dot"></span><span id="healthline">Waiting for optimizer</span></div></div></header>
<main><div id="notice" role="status"></div>
<section class="kpis" aria-label="Optimizer overview">
<div class="kpi"><span>Optimizer mode</span><strong id="mode">—</strong></div>
<div class="kpi"><span>Managed BGP routes</span><strong id="routes">—</strong></div>
<div class="kpi"><span>Observed /24 networks</span><strong id="networks">—</strong></div>
<div class="kpi"><span>Pending / candidate</span><strong id="pending">—</strong></div>
<div class="kpi"><span>Last completed cycle</span><strong id="age">—</strong></div>
</section>
<section class="healthgrid" aria-label="System health">
<div class="healthbox"><label>IPFIX collector · UCG exporter</label><strong id="ipfix">Waiting</strong><small id="ipfix-desc">No reading yet</small></div>
<div class="healthbox"><label>ExaBGP · UCG peer</label><strong id="bgp">Waiting</strong><small id="bgp-desc">Waiting for peer status</small></div>
<div class="healthbox"><label>Path probes</label><strong id="probe">—</strong><small id="probe-desc">Paired network measurements</small></div>
<div class="healthbox"><label>Optimizer runtime</label><strong id="runtime">—</strong><small id="runtime-desc">Seconds per observation cycle</small></div>
</section>
<section class="panel"><div class="panelhead"><div><h2>ISP path quality</h2><p>Per-cycle samples across the same successfully probed /24s; median RTT/jitter and mean probe loss, not an ISP-wide SLA</p></div><div class="buttons"><select id="history-period" aria-label="Graph time range"><option value="3600">1 hour</option><option value="21600">6 hours</option><option value="86400" selected>24 hours</option></select></div></div>
<div class="metric-grid"><div class="metric-card"><h3>Round-trip latency</h3><div class="legend"><span><i class="swatch fr"></i>Frontier <b id="latest-rtt-fr">—</b></span><span><i class="swatch sp"></i>Spectrum <b id="latest-rtt-sp">—</b></span></div><div id="chart-rtt" class="chart-area"></div><div class="chart-footer"><span id="rtt-samples">No history yet</span><span>milliseconds</span></div></div>
<div class="metric-card"><h3>Jitter</h3><div class="legend"><span><i class="swatch fr"></i>Frontier <b id="latest-jitter-fr">—</b></span><span><i class="swatch sp"></i>Spectrum <b id="latest-jitter-sp">—</b></span></div><div id="chart-jitter" class="chart-area"></div><div class="chart-footer"><span>Across comparable destinations</span><span>milliseconds</span></div></div>
<div class="metric-card"><h3>Probe packet loss</h3><div class="legend"><span><i class="swatch fr"></i>Frontier <b id="latest-loss-fr">—</b></span><span><i class="swatch sp"></i>Spectrum <b id="latest-loss-sp">—</b></span></div><div id="chart-loss" class="chart-area"></div><div class="chart-footer"><span>TCP/ICMP probe failures</span><span>percent</span></div></div></div>
</section>
<section class="panel"><div class="panelhead"><div><h2>Route distribution</h2><p>Only routes explicitly managed by this optimizer, not all UCG WAN routes</p></div><span class="muted" id="route-total">—</span></div><div class="summary"><div><div class="route-line"><span>Frontier</span><strong id="frontier-count" style="color:var(--frontier)">0</strong></div><div class="route-line"><span>Spectrum</span><strong id="spectrum-count" style="color:var(--spectrum)">0</strong></div><div class="bar" aria-label="Installed route distribution"><div id="frontier-bar" class="fr"></div><div id="spectrum-bar" class="sp"></div></div><span class="route-caption">Dry-run suggestions are not counted as installed routes.</span></div><div><div class="route-line"><span>Route utilization</span><strong id="utilization">—</strong></div><div class="route-line"><span>Observed WAN vs installed WAN</span><span class="route-caption">Independent measurements</span></div><div class="route-line"><span>Maximum managed routes</span><strong id="max-routes">—</strong></div></div></div></section>
<section class="panel"><div class="panelhead"><div><h2>Destination networks</h2><p>Current measurement cycle · automatically refreshed every 15 seconds</p></div><div class="buttons"><input id="search" type="search" placeholder="Search prefix or decision" aria-label="Search destination networks"><select id="filter" aria-label="Network filter"><option value="all">All</option><option value="candidate">Candidates & pending</option><option value="installed">Installed routes</option><option value="problem">Probe problems</option></select><button id="refresh" type="button">Refresh</button></div></div><div class="scroll"><table><thead><tr><th>Destination /24</th><th class="num">Traffic MiB</th><th class="num">Hosts</th><th>Observed</th><th>Installed</th><th>Frontier RTT / Loss / Score</th><th>Spectrum RTT / Loss / Score</th><th>Decision</th></tr></thead><tbody id="rows"><tr><td colspan="8" class="empty">Waiting for measurements…</td></tr></tbody></table></div></section>
<div class="two"><section class="panel"><div class="panelhead"><div><h2>Route-change history</h2><p>Persistent log of actual accepted BGP announce, move and withdraw requests</p></div></div><div id="route-events" class="timeline"><div class="empty">No live changes recorded</div></div></section>
<section class="panel"><div class="panelhead"><div><h2>Recent optimizer decisions</h2><p>Candidate and pending transitions; includes dry-run decisions</p></div></div><div id="decisions" class="timeline"><div class="empty">No decisions yet</div></div></section></div>
<footer id="footer">Read-only monitoring. This web interface does not modify BGP routes.</footer></main>
<script>
'use strict';
let snapshot=null, history=[], routeEvents=[];
const $=id=>document.getElementById(id);
function node(tag,value,cls){const e=document.createElement(tag);if(value!==undefined)e.textContent=String(value);if(cls)e.className=cls;return e}
function td(tr,value,cls){const e=node('td',value,cls);tr.appendChild(e);return e}
function num(v,places=1){return v!==null&&v!==undefined&&Number.isFinite(Number(v))?Number(v).toFixed(places):'—'}
function metric(m){return m&&m.score!==null?`${num(m.latency)}ms / ${num(m.loss,0)}% / ${num(m.score)}`:'FAIL'}
function ago(sec){if(sec===null||sec===undefined||!Number.isFinite(sec))return 'Unknown';if(sec<60)return `${Math.floor(sec)}s ago`;if(sec<3600)return `${Math.floor(sec/60)}m ago`;return `${Math.floor(sec/3600)}h ago`}
function kind(a){a=String(a||'');if(/FAILED|UNPROBEABLE|CONFLICT|INSUFFICIENT|CURRENT PATH FAILED/i.test(a))return 'bad';if(/PENDING|CANDIDATE|WOULD/i.test(a))return 'warn';if(/^(ROUTE|KEEP|WITHDRAW)/.test(a))return 'good';return 'info'}
function badge(id,value,klass){$(id).textContent=value;$(id).className=klass||''}
function renderTable(){const body=$('rows');body.replaceChildren();const rows=Array.isArray(snapshot?.rows)?snapshot.rows:[];const q=$('search').value.toLowerCase();const f=$('filter').value;let count=0;for(const r of rows){const action=String(r.action||'');const corpus=[r.prefix,action,r.observed,r.route].join(' ').toLowerCase();if(q&&!corpus.includes(q))continue;if(f==='candidate'&&!/PENDING|CANDIDATE|WOULD/.test(action))continue;if(f==='installed'&&(!r.route||r.route==='-'))continue;if(f==='problem'&&!/FAILED|UNPROBEABLE|CONFLICT|INSUFFICIENT|CURRENT PATH FAILED/.test(action))continue;const tr=node('tr');td(tr,r.prefix,'mono');td(tr,num(r.mib,2),'num');td(tr,r.hosts,'num');td(tr,r.observed||'—');td(tr,r.route&&r.route!=='-'?r.route:'—');td(tr,metric(r.frontier));td(tr,metric(r.spectrum));td(tr,action,kind(action));tr.title=(r.representatives||[]).join(', ');body.appendChild(tr);count++}if(!count){const tr=node('tr');const c=td(tr,'No matching networks','empty');c.colSpan=8}}
function setEventList(id,items,routeChanges){const root=$(id);root.replaceChildren();if(!items.length){root.appendChild(node('div',routeChanges?'No live route changes yet. Dry-run suggestions appear under Recent decisions.':'No recent decision transitions','empty'));return}for(const item of items.slice(0,40)){const c=node('div',undefined,'event');const date=node('time',item.timestamp||'');c.appendChild(date);const title=routeChanges?`${String(item.event||'').toUpperCase()} · ${item.prefix||'—'} · ${item.from||'Default'} → ${item.to||'Default'}`:`${item.prefix||'—'} · ${item.action||'—'}`;c.appendChild(node('strong',title,routeChanges?'info':kind(item.action)));if(routeChanges&&item.reason)c.appendChild(node('div',item.reason,'detail'));root.appendChild(c)}}
function renderStatus(){if(!snapshot)return;const time=Number(snapshot.epoch||0),age=Math.max(0,Date.now()/1000-time),stale=age>180,err=Boolean(snapshot.error),healthy=!stale&&!err;const mode=snapshot.mode==='APPLY'?'LIVE':'DRY-RUN';badge('mode',mode,'value '+(mode==='LIVE'?'good':'warn'));$('routes').textContent=`${snapshot.route_count||0} / ${snapshot.config?.max_routes??'—'}`;$('networks').textContent=(snapshot.rows||[]).length;$('pending').textContent=(snapshot.rows||[]).filter(r=>/PENDING|CANDIDATE|WOULD/.test(r.action||'')).length;badge('age',ago(age),stale?'bad':'');$('led').className='dot '+(healthy?(mode==='LIVE'?'ok':'dry'):'');$('healthline').textContent=err?'Optimizer error':stale?'Telemetry stale':mode==='LIVE'?'Live optimization':'Dry-run monitoring';$('notice').textContent=err?`Optimizer error: ${snapshot.error}`:stale?'Optimizer data is older than three minutes. Check the service logs.':' ';
const health=snapshot.health||{},ip=health.ipfix||{},bg=health.bgp||{},probes=health.probes||{};
badge('ipfix',ip.status==='fresh'?'Receiving':ip.status==='stale'?'Stale':ip.status==='no_data'?'No flows':'Unknown',ip.status==='fresh'?'good':'warn');$('ipfix-desc').textContent=`Most recent UCG flow: ${ip.age_seconds===null||ip.age_seconds===undefined?'unknown':ago(Number(ip.age_seconds))} · ${ip.flow_count??'—'} records`;
badge('bgp',bg.status==='established'?'Established':bg.status==='down'?'Down':'Unknown',bg.status==='established'?'good':bg.status==='down'?'bad':'warn');$('bgp-desc').textContent=bg.detail||'Neighbor summary unavailable';
badge('probe',`${probes.paired_prefixes??0} / ${probes.tested_prefixes??0}`,Number(probes.failing_prefixes||0)>0?'warn':'good');$('probe-desc').textContent=`${probes.failing_prefixes??0} unpaired/failed destinations`;
badge('runtime',num(snapshot.cycle_seconds)+'s','');$('runtime-desc').textContent=`Last successful cycle ${snapshot.timestamp||'—'}`;
const routes=Array.isArray(snapshot.routes)?snapshot.routes:[],fr=routes.filter(r=>r.isp==='FRONTIER').length,sp=routes.filter(r=>r.isp==='SPECTRUM').length,total=routes.length,max=Number(snapshot.config?.max_routes||0);$('frontier-count').textContent=fr;$('spectrum-count').textContent=sp;$('frontier-bar').style.width=total?`${fr/total*100}%`:'0%';$('spectrum-bar').style.width=total?`${sp/total*100}%`:'0%';$('route-total').textContent=`${total} optimizer-managed /24s`;$('utilization').textContent=max?`${num(total/max*100,0)}%`:'—';$('max-routes').textContent=max||'—';setEventList('decisions',snapshot.events||[],false);renderTable();$('footer').textContent=`Read-only monitoring · optimizer v${snapshot.version||'—'} · status refresh 15s · last cycle ${snapshot.timestamp||'—'}`}
function svgEl(tag,attrs){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs||{}))e.setAttribute(k,String(v));return e}
function drawGraph(containerId,key,scaleLabel){const root=$(containerId);root.replaceChildren();const seconds=Number($('history-period').value),end=Date.now()/1000,start=end-seconds;const data=history.filter(p=>Number(p.epoch)>=start&&Number(p.epoch)<=end&&p.paired_prefixes>0);const W=600,H=168,left=45,right=8,top=12,bottom=27;const svg=svgEl('svg',{viewBox:`0 0 ${W} ${H}`,preserveAspectRatio:'none',role:'img','aria-label':`Frontier and Spectrum ${scaleLabel} trend`});root.appendChild(svg);const values=[];for(const pt of data)for(const isp of ['frontier','spectrum']){const v=pt[isp]?.[key];if(v!==null&&v!==undefined&&Number.isFinite(Number(v)))values.push(Number(v))}if(!values.length){const t=svgEl('text',{x:W/2,y:H/2,'text-anchor':'middle',fill:'#92a2b8','font-size':15});t.textContent='Waiting for history samples';svg.appendChild(t);return}const max=Math.max(...values);const yMax=Math.max(key==='loss_percent'?0.25:1,max*1.12);const x=e=>left+(Number(e)-start)/seconds*(W-left-right);const y=v=>top+(1-Number(v)/yMax)*(H-top-bottom);for(let i=0;i<=4;i++){const v=yMax*i/4,yy=y(v);svg.appendChild(svgEl('line',{x1:left,y1:yy,x2:W-right,y2:yy,stroke:'#2b394c','stroke-width':1}));const t=svgEl('text',{x:left-6,y:yy+4,'text-anchor':'end',fill:'#8395ad','font-size':12});t.textContent=num(v,v<10?1:0);svg.appendChild(t)}for(const [n,frac] of [[0,0],[1,.5],[2,1]]){const xx=left+frac*(W-left-right);const t=svgEl('text',{x:xx,y:H-4,'text-anchor':n===0?'start':n===2?'end':'middle',fill:'#8395ad','font-size':11});const date=new Date((start+frac*seconds)*1000);t.textContent=date.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});svg.appendChild(t)}for(const [isp,color] of [['frontier','#57b9fa'],['spectrum','#ffc17b']]){let segment=[];function flush(){if(!segment.length)return;svg.appendChild(svgEl('polyline',{points:segment.join(' '),fill:'none',stroke:color,'stroke-width':2.5,'stroke-linecap':'round','stroke-linejoin':'round'}));segment=[]}for(const pt of data){const v=pt[isp]?.[key];if(v===null||v===undefined||!Number.isFinite(Number(v))){flush();continue}segment.push(`${x(pt.epoch).toFixed(2)},${y(v).toFixed(2)}`)}flush()}}
function renderHistory(){const prefix=history.filter(p=>p.paired_prefixes>0).at(-1);for(const [name,key,digits,suffix] of [['rtt','rtt_ms',1,'ms'],['jitter','jitter_ms',1,'ms'],['loss','loss_percent',2,'%']]){for(const [isp,short] of [['frontier','fr'],['spectrum','sp']]){const v=prefix?.[isp]?.[key];$(`latest-${name}-${short}`).textContent=v==null?'—':`${num(v,digits)}${suffix}`}drawGraph(`chart-${name}`,key,name)}$('rtt-samples').textContent=history.length?`${history.length} stored cycles · latest comparable /24s: ${prefix?.paired_prefixes??0}`:'No samples yet'}
async function loadJson(url,fallback){const r=await fetch(url,{cache:'no-store'});if(r.status===503)return fallback;if(!r.ok)throw Error(`HTTP ${r.status} at ${url}`);return r.json()}
async function refresh(){try{const values=await Promise.all([loadJson('/api/status',null),loadJson('/api/history',[]),loadJson('/api/route-events',[])]);snapshot=values[0];history=Array.isArray(values[1])?values[1]:[];routeEvents=Array.isArray(values[2])?values[2]:[];if(!snapshot)throw Error('No optimizer snapshot yet');renderStatus();renderHistory();setEventList('route-events',routeEvents,true)}catch(err){$('led').className='dot';$('healthline').textContent='Dashboard unavailable';$('notice').textContent=`Unable to load monitoring data: ${err.message}`}}
$('refresh').addEventListener('click',refresh);$('search').addEventListener('input',()=>{if(snapshot)renderTable()});$('filter').addEventListener('change',()=>{if(snapshot)renderTable()});$('history-period').addEventListener('change',renderHistory);refresh();setInterval(refresh,15000);
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    server_version = 'RouteOptimizerDashboard/3.3'

    def send_result(self, status, mime, data):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            self.send_result(200, 'text/html; charset=utf-8', HTML.encode('utf-8'))
        elif path in FILES:
            file = FILES[path]
            try:
                if file.stat().st_size > MAX_JSON_BYTES:
                    raise ValueError('Monitoring file exceeded size limit')
                content = file.read_bytes()
                json.loads(content)
                self.send_result(200, 'application/json; charset=utf-8', content)
            except (OSError, ValueError):
                self.send_result(503, 'application/json; charset=utf-8', b'{"error":"Monitoring data not available yet"}')
        elif path == '/healthz':
            self.send_result(200, 'text/plain; charset=utf-8', b'ok\n')
        else:
            self.send_result(404, 'text/plain; charset=utf-8', b'Not found\n')

    def do_POST(self):
        self.send_result(405, 'text/plain; charset=utf-8', b'Method not allowed\n')


def main():
    parser = argparse.ArgumentParser(description='Read-only route optimizer monitoring dashboard')
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8099)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    ThreadingHTTPServer.daemon_threads = True
    with ThreadingHTTPServer((args.listen, args.port), Handler) as server:
        print(f'Read-only monitoring UI on {args.listen}:{args.port}', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()