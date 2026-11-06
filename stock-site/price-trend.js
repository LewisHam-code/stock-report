(function(root){
  'use strict';
  const valid=n=>typeof n==='number'&&Number.isFinite(n)&&n>0;
  const priceConflict=cross=>cross?.fields?.price?.matches===false&&cross?.price_resolution?.status!=='resolved';
  // This is the arithmetic change of the displayed quote, not a verification result.
  function change(price,baseline){return valid(price)&&valid(baseline)?Math.round((price/baseline-1)*10000)/100:null}
  function prepare(series,baseline,current,end,at=new Date()){
    const start=String(baseline?.generated_at||'').slice(0,10),origin=Date.parse(baseline?.generated_at);
    if(!valid(baseline?.price)||!Number.isFinite(origin))return {state:'missing_baseline',points:[]};
    const initial={date:start,at:baseline.generated_at,close:baseline.price,kind:'initial'};
    const points=[initial];
    // Raw captured quote and adjusted daily closes must never share a line.
    const usable=series?.adjustment==='none'&&series?.cross_validation?.status!=='conflict';
    const dates=new Map();
    for(const point of usable?series.points||[]:[]){
      const closeAt=point.date+'T15:00:00+08:00',time=Date.parse(closeAt);
      if(/^\d{4}-\d{2}-\d{2}$/.test(point.date)&&point.date>=start&&point.date<=end&&time>origin&&time<=at.getTime()&&valid(point.close))dates.set(point.date,{date:point.date,at:closeAt,close:point.close,kind:'close'});
    }
    const quoteTime=Date.parse(current?.current_asof),quoteDay=String(current?.current_asof||'').slice(0,10);
    const quoteConflict=priceConflict(current?.current_cross_validation);
    if(valid(current?.current_price)&&quoteTime>origin&&quoteTime<=at.getTime()&&quoteDay<=end){
      const saved=dates.get(quoteDay),closeTime=Date.parse(quoteDay+'T15:00:00+08:00');
      // A stale intraday quote cannot replace a stored closing price.
      if(!saved||quoteTime>=closeTime)dates.set(quoteDay,{date:quoteDay,at:current.current_asof,close:current.current_price,kind:quoteTime>=closeTime?'close':'latest',priceConflict:quoteConflict});
    }
    points.push(...[...dates.values()].sort((a,b)=>Date.parse(a.at)-Date.parse(b.at)));
    return {state:'available',points,dailyState:usable?'available':series?.cross_validation?.status==='conflict'?'conflict':'pending'};
  }
  function create(series,baseline,current,end){
    const result=prepare(series,baseline,current,end),container=document.createElement('div');
    if(result.state!=='available'){
      const message=document.createElement('p');message.className='subtle';
      message.textContent='解读时股价或解读时间未留存，暂不能确定走势图起点。';
      container.appendChild(message);return container;
    }
    const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');
    const points=result.points,values=points.map(p=>p.close),low=Math.min(...values),high=Math.max(...values),pad=Math.max((high-low)*.12,high*.002,.01);
    const min=low-pad,max=high+pad,left=65,right=24,width=Math.max(720,left+right+(points.length-1)*120),height=280,top=40,bottom=38;
    const x=i=>points.length===1?(left+width-right)/2:left+i/(points.length-1)*(width-left-right);
    const y=v=>top+(max-v)/(max-min)*(height-top-bottom);
    const shape=(tag,attrs,text)=>{const node=document.createElementNS(ns,tag);for(const [key,value] of Object.entries(attrs))node.setAttribute(key,String(value));if(text!=null)node.textContent=text;svg.appendChild(node);return node};
    svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.setAttribute('role','img');svg.classList.add('price-trend-chart');
    const description=`${points[0].date}解读时股价${points[0].close.toFixed(2)}元起，至${points.at(-1).date}，${points.length}个价格点，最低${low.toFixed(2)}元，最高${high.toFixed(2)}元。`;
    svg.setAttribute('aria-label',description);shape('title',{},description);
    for(let i=0;i<=4;i++){
      const value=min+(max-min)*i/4,yy=y(value);
      shape('line',{x1:left,y1:yy,x2:width-right,y2:yy,stroke:'#e5e9ef'});
      shape('text',{x:left-10,y:yy+4,'text-anchor':'end',fill:'#667085','font-size':12},value.toFixed(2));
    }
    shape('text',{x:left,y:height-12,fill:'#667085','font-size':12},points[0].date);
    if(points.length>1)shape('text',{x:width-right,y:height-12,'text-anchor':'end',fill:'#667085','font-size':12},points.at(-1).date);
    shape('line',{x1:left,y1:y(points[0].close),x2:width-right,y2:y(points[0].close),stroke:'#98a2b3','stroke-dasharray':'4 4'});
    const color=points.at(-1).close===points[0].close?'#667085':points.at(-1).close>points[0].close?'#a44c3f':'#0b7662';
    if(points.length>1)shape('polyline',{points:points.map((p,i)=>`${x(i)},${y(p.close)}`).join(' '),fill:'none',stroke:color,'stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'});
    points.forEach((point,i)=>{
      const percent=change(point.close,points[0].close);
      const percentText='涨幅 '+(percent>0?'+':'')+percent.toFixed(2)+'%';
      const pointColor=percent===0?'#667085':percent>0?'#a44c3f':'#0b7662';
      const circle=shape('circle',{cx:x(i),cy:y(point.close),r:points.length===1?5:4,fill:color,'fill-opacity':points.length===1||i===points.length-1?1:.15});
      const title=document.createElementNS(ns,'title');title.textContent=point.at+' · '+({initial:'解读时股价',close:'收盘价',latest:'盘中最新价'}[point.kind])+' '+point.close.toFixed(2)+' 元 · '+percentText+(point.priceConflict?' · 双源价格有差异，待核对':'');circle.appendChild(title);
      if(point.priceConflict){circle.setAttribute('fill','#fff');circle.setAttribute('fill-opacity','1');circle.setAttribute('stroke',color);circle.setAttribute('stroke-width','2')}
      const latest=i===points.length-1;
      const attrs={x:x(i),y:y(point.close)-30,'text-anchor':points.length===1?'middle':i===0?'start':latest?'end':'middle',fill:'#344054','font-size':12,'font-weight':600,stroke:'#fff','stroke-width':3,'paint-order':'stroke','stroke-linejoin':'round'};
      shape('text',{...attrs,'data-price-label':point.date},point.close.toFixed(2)+' 元');
      shape('text',{...attrs,y:y(point.close)-12,fill:pointColor,'data-change-label':point.at},percentText);
    });
    const scroll=document.createElement('div');scroll.style.overflowX='auto';
    svg.style.minWidth=width+'px';scroll.appendChild(svg);container.appendChild(scroll);
    const note=document.createElement('p');note.className='subtle';
    note.textContent=(points.length===1?'目前仅有解读时股价，后续价格取得后自动形成走势。':`从解读时股价 ${points[0].close.toFixed(2)} 元至 ${points.at(-1).close.toFixed(2)} 元；涨幅 ${change(points.at(-1).close,points[0].close).toFixed(2)}%。`)+(result.dailyState==='conflict'?' 日线来源存在差异，冲突日线未绘入。':result.dailyState==='pending'?' 每日收盘价待补齐，已保存的起点及最新报价先行显示。':'');
    if(points.some(point=>point.priceConflict))note.textContent+=' 最新报价双源有差异，空心节点及其涨幅暂按页面显示的报价计算，待核对。';
    container.appendChild(note);return container;
  }
  root.StockPriceTrend={prepare,create,change};
})(typeof window==='undefined'?globalThis:window);
