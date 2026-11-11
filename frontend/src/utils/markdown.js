/* 轻量 Markdown 渲染（离线内置，先转义防 XSS）——从原生版原样移植 */
export function esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escInline(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function inlineMd(s){
  s = escInline(s);
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s;
}
export function renderMarkdown(src){
  if(!src) return '';
  const lines = String(src).replace(/\r\n/g,'\n').split('\n');
  let html = '', i = 0, inCode = false, codeBuf = [], listType = null, tableBuf = [];

  function closeList(){ if(listType){ html += '</' + listType + '>'; listType = null; } }
  function closeTable(){
    if(tableBuf.length){
      let t = '<table><thead><tr>';
      tableBuf[0].forEach(c => t += '<th>' + inlineMd(c) + '</th>');
      t += '</tr></thead><tbody>';
      for(let r = 1; r < tableBuf.length; r++){
        t += '<tr>';
        tableBuf[r].forEach(c => t += '<td>' + inlineMd(c) + '</td>');
        t += '</tr>';
      }
      t += '</tbody></table>';
      html += t; tableBuf = [];
    }
  }

  while(i < lines.length){
    const line = lines[i];
    if(inCode){
      if(line.trim().startsWith('```')){ inCode = false; html += '<pre><code>' + escInline(codeBuf.join('\n')) + '</code></pre>'; codeBuf = []; i++; continue; }
      codeBuf.push(line); i++; continue;
    }
    if(line.trim().startsWith('```')){
      closeList(); closeTable();
      inCode = true; codeBuf = []; i++; continue;
    }
    const t = line.trim();
    if(!t){ closeList(); closeTable(); i++; continue; }

    if(t.startsWith('|') && i + 1 < lines.length && /^\|?[\s:|-]+\|?\s*$/.test(lines[i+1].trim()) && t.includes('|')){
      closeList();
      tableBuf = [t.split('|').slice(1,-1).map(s => s.trim())];
      i += 2;
      while(i < lines.length && lines[i].trim().startsWith('|')){
        tableBuf.push(lines[i].split('|').slice(1,-1).map(s => s.trim()));
        i++;
      }
      closeTable(); continue;
    }
    closeTable();

    const hm = t.match(/^(#{1,4})\s+(.*)$/);
    if(hm){ closeList(); html += '<h' + hm[1].length + '>' + inlineMd(hm[2]) + '</h' + hm[1].length + '>'; i++; continue; }
    if(t.startsWith('>')){
      closeList();
      const q = [];
      while(i < lines.length && lines[i].trim().startsWith('>')){ q.push(lines[i].trim().replace(/^>\s?/,'')); i++; }
      html += '<blockquote>' + q.map(l => '<p>' + inlineMd(l) + '</p>').join('') + '</blockquote>'; continue;
    }
    if(/^(-{3,}|\*{3,}|_{3,})$/.test(t)){ closeList(); html += '<hr>'; i++; continue; }
    const um = t.match(/^[-*+]\s+(.*)$/);
    if(um){
      if(listType !== 'ul'){ closeList(); html += '<ul>'; listType = 'ul'; }
      html += '<li>' + inlineMd(um[1]) + '</li>'; i++; continue;
    }
    const om = t.match(/^\d+[.、)]\s+(.*)$/);
    if(om){
      if(listType !== 'ol'){ closeList(); html += '<ol>'; listType = 'ol'; }
      html += '<li>' + inlineMd(om[1]) + '</li>'; i++; continue;
    }
    closeList();
    html += '<p>' + inlineMd(t) + '</p>';
    i++;
  }
  if(inCode) html += '<pre><code>' + escInline(codeBuf.join('\n')) + '</code></pre>';
  closeList(); closeTable();
  return '<div class="md">' + html + '</div>';
}
