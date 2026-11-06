(function(root){
  'use strict';
  const PAGE_SIZE=30;
  function parsePage(value){
    const number=Number(value);
    return Number.isSafeInteger(number)&&number>0?number:1;
  }
  function paginate(items,requested,size=PAGE_SIZE){
    const total=items.length;
    const pages=Math.max(1,Math.ceil(total/size));
    const page=Math.min(parsePage(requested),pages);
    const start=(page-1)*size;
    return {items:items.slice(start,start+size),total,pages,page,
      from:total?start+1:0,to:Math.min(start+size,total)};
  }
  root.StockPagination={PAGE_SIZE,parsePage,paginate};
})(typeof window==='undefined'?globalThis:window);
