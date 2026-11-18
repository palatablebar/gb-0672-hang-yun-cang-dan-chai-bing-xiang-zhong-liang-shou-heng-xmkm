const ui={busy:false};const output=document.querySelector('#message');document.querySelector('#print').addEventListener('click',()=>window.print());document.querySelector('#execute').addEventListener('click',async()=>{if(ui.busy)return;ui.busy=true;output.textContent='核验中…';try{const dataset=document.querySelector('#sample').value;const response=await fetch(dataset===document.body.dataset.source?'/api/runs/'+document.body.dataset.runId+'/rerun':'/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset})});const result=await response.json();if(!response.ok)throw Error(result.detail);const url=new URL(location.href);url.searchParams.set('dataset',dataset);url.searchParams.set('run_id',result.manifest.run_id);location.href=url;}catch(e){output.textContent=e.message;}finally{ui.busy=false;}});document.querySelector('#query').addEventListener('input',event=>document.querySelectorAll('.data-panel tbody tr').forEach(row=>row.hidden=!row.textContent.toLowerCase().includes(event.target.value.toLowerCase())));document.querySelector('#sample').value=new URL(location.href).searchParams.get('dataset')||document.querySelector('#sample').value;
const filePicker=document.querySelector('#csv-file');
if(filePicker){
 const editor=document.querySelector('#csv-text');
 filePicker.addEventListener('change',async()=>{const file=filePicker.files[0];if(!file)return;if(file.size>2000000){editor.value='';alert('文件超过2MB，请缩小后导入');return;}const bytes=await file.arrayBuffer();try{editor.value=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch(e){editor.value=new TextDecoder('gb18030',{fatal:true}).decode(bytes);}});
 document.querySelector('#import-csv').addEventListener('click',async event=>{event.target.disabled=true;try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv_text:editor.value})});const data=await response.json();if(!response.ok)throw Error(data.detail);location.href=location.pathname+'?run_id='+data.manifest.run_id;}catch(e){editor.setCustomValidity(e.message);editor.reportValidity();}finally{event.target.disabled=false;}});
 editor.addEventListener('input',()=>editor.setCustomValidity(''));
}

// 版本封存工作台：逐张主单复核、别单补证裁决、交接封存
const workbench=document.querySelector('.worklist-panel');
if(workbench){
 const runId=document.body.dataset.runId;
 const msg=document.querySelector('#review-message');
 const revision=()=>Number(document.querySelector('.worklist-panel h2 small').dataset.revision);
 const post=async(url,body)=>{
   const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
   const data=await response.json();
   if(!response.ok)throw Error(data.detail||'操作失败');
   return data;
 };
 document.querySelectorAll('.confirm-root').forEach(btn=>btn.addEventListener('click',async()=>{
   btn.disabled=true;msg.textContent='提交中…';
   try{
     await post(`/api/runs/${runId}/roots/${btn.dataset.root}/confirm`,{revision:revision(),request_token:btn.dataset.root+':'+Date.now()});
     location.reload();
   }catch(e){msg.textContent=e.message;btn.disabled=false;}
 }));
 document.querySelector('#confirm-batch').addEventListener('click',async event=>{
   event.target.disabled=true;msg.textContent='整批确认中…';
   try{
     await post(`/api/runs/${runId}/confirm-batch`,{revision:revision(),request_token:'batch:'+Date.now()});
     location.reload();
   }catch(e){msg.textContent=e.message;event.target.disabled=false;}
 });
 document.querySelector('#seal-handoff').addEventListener('click',async event=>{
   event.target.disabled=true;msg.textContent='生成交接封存件…';
   try{
     await post(`/api/runs/${runId}/seal`,{revision:revision(),request_token:'seal:'+Date.now()});
     location.reload();
   }catch(e){msg.textContent=e.message;event.target.disabled=false;}
 });
 document.querySelector('#supplement-submit').addEventListener('click',async event=>{
   const text=document.querySelector('#supplement-text').value.trim();
   if(!text){msg.textContent='请粘贴补证后的完整 CSV';return;}
   event.target.disabled=true;msg.textContent='补证裁决中…';
   try{
     await post(`/api/runs/${runId}/supplement`,{revision:revision(),csv_text:text,request_token:'supplement:'+Date.now()});
     location.reload();
   }catch(e){msg.textContent=e.message;event.target.disabled=false;}
 });
}
