const $=s=>document.querySelector(s);
let sid=sessionStorage.getItem('yanwei_session')||'',asset=null,busy=false;
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el}
function message(role,text,result){
 const el=node('div',undefined,'bubble '+(role==='你'?'user':''));el.append(node('span',role,'role'),node('div',text));
 if(result){
  const semantic=result.multimodal?.used;
  if((result.action==='RETURN_CASES'||semantic)&&result.candidates?.length){
   el.append(node('p',result.action==='RETURN_CASES'?'研究用候选病例':'图像相似候选 · 信息尚未齐全，需补充确认','note'));
   const grid=node('div',undefined,'gallery');
   for(const c of result.candidates){
    const card=node('article',undefined,'case-card'),id=c.source?.case_id||c.region_id||'候选病例';
    const image=node('img');image.src=c.image_url;image.alt=id+'的眼底原图预览';image.loading='lazy';
    image.onerror=()=>{image.hidden=true;card.prepend(node('p','该候选图片暂不可用','note'))};
    card.append(image,node('strong',id),node('p',Object.values(c.profile_display||{}).join(' · '),'note'));
    if(c.semantic_scores){const s=c.semantic_scores;card.append(node('p',(result.multimodal?.query_kind==='image_text'?'图文联合余弦相似度 ':result.multimodal?.query_kind==='text'?'文字-图像余弦相似度 ':'整图余弦相似度 ')+Number(s.cosine).toFixed(3),'note'))}
    if(c.region_image_url){const link=node('a','查看参考区域（元数据，待复核）');link.href=c.region_image_url;link.target='_blank';link.rel='noopener';link.title=c.region_note;card.append(link)}
    card.append(node('p',c.source?.official_source||'来源详见证据卡','note'));grid.append(card);
   }el.append(grid);
  }
  const url=result.evidence?.view_url;if(url){const link=node('a','查看证据卡与限制说明');link.href=url;link.target='_blank';link.rel='noopener';el.append(link)}
  const detail=node('details');detail.append(node('summary','查看本轮状态'),node('pre',JSON.stringify({action:result.action,耗时毫秒:result.elapsed_ms,图文检索:result.multimodal,trace_id:result.trace_id},null,2)));el.append(detail);
 }$('#messages').append(el);el.scrollIntoView({block:'nearest'});
}
async function post(path,body){const res=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await res.json();if(!res.ok||data.ok===false)throw Error(data.error?.message||data.result?.action_reason||'请求失败');return data}
function lock(value){busy=value;for(const b of document.querySelectorAll('button,input,textarea'))b.disabled=value}
$('#file').onchange=async e=>{const file=e.target.files[0];if(!file)return;if(file.size>8*1024*1024){$('#status').textContent='图片不得超过8MB';return}lock(true);$('#status').textContent='正在校验并保存图片…';try{const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file)});const data=await post('/api/upload',{filename:file.name,data:encoded});asset=data.asset.asset_id;$('#preview').src=data.asset.preview_url;$('#preview').hidden=false;$('#filename').textContent=file.name;$('#status').textContent=data.asset.notice}catch(err){$('#status').textContent=err.message}finally{lock(false)}};
$('#form').onsubmit=async e=>{e.preventDefault();if(busy)return;const text=$('#text').value.trim();if(!text&&!asset)return;message('你',text||'查找与这张图片相似的病例');lock(true);$('#status').textContent='正在检索…';try{const data=await post('/api/chat',{text,session_id:sid||undefined,asset_id:asset||undefined});sid=data.session_id;sessionStorage.setItem('yanwei_session',sid);asset=null;$('#text').value='';message('眼维',data.reply,data.result);$('#status').textContent='本轮已完成'}catch(err){$('#status').textContent='本轮失败：'+err.message}finally{lock(false)}};
$('#reset').onclick=()=>{sid='';asset=null;sessionStorage.removeItem('yanwei_session');$('#messages').replaceChildren();$('#preview').hidden=true;$('#file').value='';$('#filename').textContent='图片不超过8MB';$('#status').textContent='已新建会话';message('眼维','可以上传一张眼底图片，或先描述病灶。')};
message('眼维',sid?'已恢复本页会话，可继续补充描述。':'上传眼底图片即可查看相似候选，也可以补充文字缩小范围。');
fetch('/api/multimodal').then(r=>r.json()).then(info=>{const badge=node('p',info.ready?'图文向量检索已启用 · '+info.image_count+'张原图':'当前使用文字检索 · '+info.reason,'note');$('header').after(badge)}).catch(()=>{});
