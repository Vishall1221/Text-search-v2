const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
function setup(){
  const nodes={},requests=[];
  class Element{
    constructor(){this.handlers={};this.children=[];this.textContent='';this.value='';}
    append(...items){this.children.push(...items);}
    replaceChildren(...items){this.children=items;}
    addEventListener(name,fn){this.handlers[name]=fn;}
  }
  const get=id=>nodes[id]??=new Element();
  const context=vm.createContext({document:{querySelector:get,createElement:()=>new Element()},AbortController,
    fetch:(_,options)=>new Promise(resolve=>requests.push({options,resolve}))});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8'),context);
  return {nodes,requests,submit:q=>{get('#query').value=q;return get('#search').handlers.submit({preventDefault(){}});}};
}
const response=message=>({ok:true,json:async()=>({status:'ok',message,results:[],notes:[],interpretation:{filters:[]},timings_ms:{total:1}})});
test('late results do not overwrite the newest search',async()=>{
 const {nodes,requests,submit}=setup();const a=submit('first'),b=submit('second');
 assert.equal(requests[0].options.signal.aborted,true);
 requests[1].resolve(response('SECOND'));await b;
 requests[0].resolve(response('FIRST'));await a;
 assert.match(nodes['#status'].textContent,/SECOND/);
});
test('metadata and notes are inserted as text',async()=>{
 const {nodes,requests,submit}=setup();const pending=submit('test');
 requests[0].resolve({ok:true,json:async()=>({status:'ok',results:[],notes:['<script>bad()</script>'],interpretation:{filters:[]},timings_ms:{total:1}})});
 await pending;assert.equal(nodes['#notes'].children[0].textContent,'<script>bad()</script>');
 assert.equal(nodes['#notes'].children[0].innerHTML,undefined);
});
