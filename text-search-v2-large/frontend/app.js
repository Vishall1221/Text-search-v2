const $=s=>document.querySelector(s);
const node=(tag,text)=>{const n=document.createElement(tag); n.textContent=text??'';return n;};
let identity=0,controller;
$('#search').addEventListener('submit',async event=>{
  event.preventDefault();const id=++identity;controller?.abort();controller=new AbortController();
  $('#status').textContent='Searching…';$('#results').replaceChildren();$('#notes').replaceChildren();
  $('#interpretation').textContent='';
  try {
    const response=await fetch('/api/search',{method:'POST',headers:{'Content-Type':'application/json'},
      signal:controller.signal,body:JSON.stringify({query:$('#query').value,limit:10})});
    const data=await response.json();if(id!==identity)return;
    if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Please check your search input.');
    $('#status').textContent=`${data.status.replaceAll('_',' ')} · ${data.results.length} movies · ${data.timings_ms.total??0} ms${data.message?' — '+data.message:''}`;
    $('#interpretation').textContent=(data.interpretation.filters??[]).join(' · ');
    for(const note of data.notes??[])$('#notes').append(node('li',note));
    for(const name of data.clarification?.suggestions??[]){const b=node('button',name);b.addEventListener('click',()=>{$('#query').value=name;$('#search').requestSubmit();});$('#results').append(b);}
    for(const movie of data.results){const card=node('article');card.append(node('h2',`${movie.rank}. ${movie.title}`),
      node('p',`Audio: ${movie.audio_languages.join(', ')} · ${movie.genres.join(', ')}${movie.release_date?' · ZEE5: '+movie.release_date:''}`),
      node('p',movie.plot_summary),node('p',`Cast: ${movie.cast.join(', ')}`),node('p',`Themes: ${movie.themes.join(', ')}`));$('#results').append(card);}
  }catch(error){if(id===identity&&error.name!=='AbortError')$('#status').textContent=error.message;}
});
