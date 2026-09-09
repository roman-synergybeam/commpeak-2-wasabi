/* Minimal DOM stub — only what data-table.js actually touches. */
class ClassList {
  constructor(){ this.s = new Set(); }
  toggle(c, on){ on ? this.s.add(c) : this.s.delete(c); }
  contains(c){ return this.s.has(c); }
}
class El {
  constructor(tag='div', data={}){
    this.tagName = tag.toUpperCase(); this.dataset = {...data};
    this.children = []; this.hidden = false; this.classList = new ClassList();
    this.textContent = ''; this.listeners = {}; this.parentNode = null;
    this.options = []; this.value = '';
  }
  appendChild(c){
    if (c.parentNode) c.parentNode.children = c.parentNode.children.filter(x=>x!==c);
    c.parentNode = this; this.children.push(c); return c;
  }
  addEventListener(k,f){ (this.listeners[k] ||= []).push(f); }
  fire(k){ (this.listeners[k]||[]).forEach(f=>f({target:this})); }
  querySelector(){ return null; }
  querySelectorAll(){ return []; }
  setAttribute(){} 
}
const rowsHost = new El('tbody');
const ROWS = [
  {id:'a', search:'alpha', tenant:'x', size:'300'},
  {id:'b', search:'bravo', tenant:'y', size:'100'},
  {id:'c', search:'charlie', tenant:'x', size:'200'},
  {id:'d', search:'delta', tenant:'y', size:'500'},
  {id:'e', search:'echo', tenant:'x', size:'400'},
].map(d => { const r = new El('tr', d); rowsHost.appendChild(r); return r; });

const table = new El('table');
table.querySelectorAll = (sel) => sel === 'tr.row' ? rowsHost.children : [];
const pagebtns = new El('div'), range = new El('span');
const pager = new El('div');
pager.querySelector = (s) => s === '.pagebtns' ? pagebtns : s === '.range' ? range : null;
const per = new El('select'); per.value = '2'; per.options = [{value:'2'},{value:'5'}];
const search = new El('input'); search.value = '';

global.document = {
  querySelector: () => null, querySelectorAll: () => [],
  createElement: (t) => new El(t),
};
global.localStorage = { getItem: () => null, setItem: () => {} };

const {createDataTable, pageWindow} = await import('../js/data-table.js');

let fails = 0;
const eq = (name, got, want) => {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { console.log(`FAIL ${name}\n  got  ${g}\n  want ${w}`); fails++; }
  else console.log(`ok   ${name}`);
};

// --- pageWindow ---
eq('pageWindow small',  pageWindow(1, 3), [1,2,3]);
eq('pageWindow elides', pageWindow(5, 10), [1,null,4,5,6,null,10]);
eq('pageWindow at end', pageWindow(10,10), [1,null,9,10]);

// --- paging ---
const dt = createDataTable({
  table, rows:'tr.row', rowContainer: table,
  search: {el: search, key:'search'},
  page: {el: pager, per, defaultSize:2, storageKey:null},
});
const visibleIds = () => rowsHost.children.filter(r=>!r.hidden).map(r=>r.dataset.id);
eq('page 1 of 5 @2', visibleIds(), ['a','b']);
eq('range text', range.textContent, 'Showing 1–2 of 5');
eq('lastrow on b', rowsHost.children.map(r=>r.classList.contains('lastrow')), [false,true,false,false,false]);

// go to page 3 (the partial one)
const btns = pagebtns.children.filter(c=>c.tagName==='BUTTON');
btns.find(b=>b.textContent==='3').fire('click');
eq('page 3 is partial', visibleIds(), ['e']);
eq('range text p3', range.textContent, 'Showing 5–5 of 5');

// --- filtering resets to page 1 and re-slices ---
search.value = 'a';                    // alpha, bravo, charlie, delta
search.fire('input');
eq('filtered set page1', visibleIds(), ['a','b']);
eq('filtered range', range.textContent, 'Showing 1–2 of 4');

search.value = 'zzz';
search.fire('input');
eq('no match hides all', visibleIds(), []);
eq('no-match message', range.textContent, 'Nothing matches these filters.');

// --- sorting ---
search.value = ''; search.fire('input');
dt.sortBy('size','num');               // first click on num = desc
eq('sorted desc by size', rowsHost.children.map(r=>r.dataset.id), ['d','e','a','c','b']);
dt.sortBy('size','num');               // toggles asc
eq('sorted asc by size',  rowsHost.children.map(r=>r.dataset.id), ['b','c','a','e','d']);
eq('paging follows sort', visibleIds(), ['b','c']);

console.log(fails ? `\n${fails} FAILED` : '\nall passed');
process.exit(fails ? 1 : 0);
