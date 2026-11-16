'use strict';
const prefix = document.querySelector('meta[name="tardis-prefix"]').content;
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {servers: [], keys: [], backups: [], audit: [], engines: [], jobs: [], snapshots: [], copies: [], status: {}, notifications:{deliveries:[]}};
let editing = null;
let restoreState = null;
const bytes = value => { const units=['o','Kio','Mio','Gio','Tio']; let size=Number(value || 0),i=0; while(size>=1024 && i<units.length-1){size/=1024;i++;} return `${new Intl.NumberFormat('fr-FR',{maximumFractionDigits:1}).format(size)} ${units[i]}`; };

async function api(path, options = {}) {
  const headers = {'X-Tardis-Request': '1', ...options.headers};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(`${prefix}/api/v1${path}`, {...options, headers});
  if (response.status === 204) return null;
  const payload = await response.json();
  if (!response.ok) throw new Error([payload.detail, ...(payload.errors || []).map(e => `${e.field} : ${e.message}`)].join(' '));
  return payload;
}

function notice(message, success = false) {
  $('#notice').textContent = message;
  $('#notice').className = success ? 'success' : '';
  $('#notice').hidden = false;
}

function empty(title, description, kind) {
  return `<div class="empty"><div class="empty-icon">◇</div><h3>${title}</h3><p>${description}</p>${kind ? `<button data-new="${kind}">Commencer →</button>` : ''}</div>`;
}

function backupCards() {
  if (!state.backups.length) return empty('Votre première sauvegarde commence ici', 'Ajoutez un serveur, importez une clé SSH, puis choisissez les dossiers à conserver.', 'backup');
  return `<div class="list">${state.backups.map(b => {
    const server = state.servers.find(s => s.id === b.server_id);
    const key = state.keys.find(k => k.id === b.key_id);
    const latest = state.jobs.find(j => j.backup_id === b.id);
    const active = state.jobs.some(j => j.backup_id === b.id && ['queued','running'].includes(j.state));
    const ready = b.enabled && server?.enabled && server?.host_fingerprint && state.status.worker?.online;
    return `<article class="card"><div class="card-top"><div><h3>${esc(b.name)}</h3><p>${esc(b.login)}@${esc(server?.host)} · ${esc(key?.name)} · ${esc(b.engine)} · révision ${b.revision}</p></div><div class="card-actions"><button data-test="${b.id}" ${!ready || active ? 'disabled' : ''}>Tester</button><button data-run="${b.id}" ${!ready || active ? 'disabled' : ''}>Sauvegarder</button><button data-edit="backup" data-id="${b.id}">Modifier</button></div></div><div class="chips">${b.folders.map(f => `<code class="chip">${esc(f)}</code>`).join('')}</div><p>${esc(b.schedule)} · ${esc(b.timezone)} · ${b.retention.daily} quotidiennes / ${b.retention.weekly} hebdo / ${b.retention.monthly} mensuelles</p><span class="state ${b.enabled ? '' : 'off'}">${!b.enabled ? 'Désactivée' : !server?.host_fingerprint ? 'Empreinte SSH à valider dans Serveurs' : !state.status.worker?.online ? 'Worker hors ligne' : latest ? esc(jobState(latest.state)) : 'Prête · sauvegarde automatique active'}</span></article>`;
  }).join('')}</div>`;
}

const events = {'server.created':'Serveur ajouté', 'server.updated':'Serveur modifié', 'server.deleted':'Serveur supprimé', 'key.imported':'Clé SSH importée', 'key.deleted':'Clé SSH supprimée', 'backup.created':'Sauvegarde configurée', 'backup.updated':'Sauvegarde modifiée'};
const jobState = value => ({queued:'En attente',running:'En cours',succeeded:'Réussie',failed:'Échouée',cancelled:'Annulée'})[value] || value;

function render() {
  for (const [kind, values] of [['servers',state.servers],['keys',state.keys],['backups',state.backups]]) {
    $(`#count-${kind}`).textContent = values.length;
    $(`#nav-${kind}`).textContent = values.length;
  }
  $('#overview-backups').innerHTML = backupCards();
  $('#backup-list').innerHTML = backupCards();
  $('#server-list').innerHTML = state.servers.length ? `<div class="list">${state.servers.map(s => `<article class="card"><div class="card-top"><div><h3>${esc(s.name)}</h3><p><code>${esc(s.host)}:${s.port}</code> · ${s.enabled ? 'Activé' : 'Désactivé'}</p><p>${state.backups.filter(b => b.server_id === s.id).length} sauvegarde(s) associée(s)</p></div><div class="card-actions"><button data-edit="server" data-id="${s.id}">Modifier</button><button data-delete="server" data-id="${s.id}">Supprimer</button></div></div></article>`).join('')}</div>` : empty('Aucun serveur enregistré', 'Ajoutez son nom et son adresse SSH. Le login sera choisi dans chaque sauvegarde.', 'server');
  $('#key-list').innerHTML = state.keys.length ? `<div class="list">${state.keys.map(k => `<article class="card"><div class="card-top"><div><h3>${esc(k.name)}</h3><p>${esc(k.key_type)} · ${k.backups.length} sauvegarde(s)</p></div><div class="card-actions"><button data-copy="${k.id}">Copier la clé publique</button><button data-delete="key" data-id="${k.id}">Supprimer</button></div></div><p><code>${esc(k.fingerprint)}</code></p><textarea class="key-public" readonly aria-label="Clé publique ${esc(k.name)}">${esc(k.public_key)}</textarea><div class="chips">${k.backups.map(b => `<span class="chip">${esc(b.name)}</span>`).join('')}</div></article>`).join('')}</div>` : empty('Vos accès SSH, au même endroit', 'Importez une clé privée existante. Son contenu restera masqué après l’import.', 'key');
  $('#audit-list').innerHTML = state.audit.length ? state.audit.map(a => `<div class="audit-row"><span>${esc(events[a.event] || a.event)}</span><code>${esc(a.resource_id.slice(0,8))}</code><time>${esc(new Date(a.created_at).toLocaleString('fr-FR'))}</time></div>`).join('') : empty('Aucune activité pour le moment', 'Les modifications de configuration apparaîtront ici.');
  // Add host approval controls without placing host keys in HTML attributes.
  document.querySelectorAll('#server-list .card').forEach((card,index) => {
    const server = state.servers[index];
    const line = document.createElement('p');
    line.textContent = server.host_fingerprint ? `Empreinte approuvée : ${server.host_fingerprint}` : 'Empreinte SSH non validée';
    card.append(line);
    const button = document.createElement('button');
    button.dataset.approveHost = server.id;
    button.textContent = server.host_fingerprint ? 'Vérifier la clé d’hôte' : 'Valider la clé d’hôte';
    card.append(button);
  });
  $('#worker-status').textContent = state.status.worker?.online ? 'Worker actif · sauvegardes planifiées' : 'Worker hors ligne · vérifier le service Docker';
  const latestCopy = state.status.database_backup?.latest;
  $('#database-status').textContent = state.status.database_backup?.error || (latestCopy ? `Dernière copie SQLite vérifiée : ${new Date(latestCopy.created_at).toLocaleString('fr-FR')} · ${state.copies.length} copie(s) conservée(s)` : 'Première copie SQLite en attente du worker.');
  $('#jobs-list').innerHTML = state.jobs.length ? `<div class="list">${state.jobs.map(j => `<article class="card"><div class="card-top"><div><h3>${esc(j.backup_name)}</h3><p>${esc(j.kind)} · ${esc(jobState(j.state))} · ${esc(new Date(j.created_at).toLocaleString('fr-FR'))}</p><p>${esc(j.error || '')}</p></div><div class="card-actions"><button data-log="${j.id}">Journal</button>${['queued','running'].includes(j.state) && j.kind !== 'database' ? `<button data-cancel-job="${j.id}">Annuler</button>` : ''}</div></div></article>`).join('')}</div>` : empty('Aucune exécution', 'Lancez un test ou une sauvegarde pour voir son résultat ici.');
  $('#snapshot-list').innerHTML = state.snapshots.length ? `<div class="list">${state.snapshots.map(s => `<article class="card"><div class="card-top"><div><h3>${esc(s.name)}</h3><p>${esc(new Date(s.created_at).toLocaleString('fr-FR'))} · ${esc(s.engine)} · révision ${s.revision}</p><p>${s.folders.map(esc).join(' · ')}</p></div><button data-browse="${s.id}">Explorer / récupérer</button></div></article>`).join('')}</div>` : empty('Aucune version publiée', 'Une version apparaît ici après une collecte complète réussie.');
  document.querySelectorAll('#snapshot-list .card').forEach((card,index) => {
    const version=state.snapshots[index];
    const button=document.createElement('button');button.dataset.verify=version.id;
    button.textContent=version.checksum_available ? 'Vérifier les SHA256' : 'Créer une référence SHA256';card.append(button);
  });
  const storage=state.status.storage;
  $('#storage-status').textContent=storage ? `Stockage TARDIS : ${bytes(storage.allocated_bytes)} occupés physiquement · ${bytes(storage.logical_bytes)} logiques · disque à ${storage.filesystem_used_percent} % (${bytes(storage.filesystem_free_bytes)} libres)` : 'Mesure du stockage en attente.';
  $('#notification-status').textContent=state.notifications.configured ? 'Webhook actif · livraison signée si un secret est configuré.' : 'Webhook désactivé. Configurez son URL dans les paramètres de déploiement.';
  $('#notification-list').innerHTML=state.notifications.deliveries.map(d => `<div class="audit-row"><span>${esc(d.event)} · ${esc(d.state)} · ${d.attempts} essai(s)</span><time>${esc(new Date(d.created_at).toLocaleString('fr-FR'))}</time></div>`).join('');
  renderRestore();
  $('#copy-list').innerHTML = state.copies.map(c => `<article class="card"><p>${esc(new Date(c.created_at).toLocaleString('fr-FR'))} · intégrité ${esc(c.integrity)} · ${c.bytes} octets</p><p><code>SHA256 ${esc(c.sha256)}</code></p><a href="${prefix}/api/v1/database-backups/${c.id}/download">Télécharger la copie SQLite ↓</a></article>`).join('');
}

async function refresh() {
  const [servers, keys, backups, audit, engines, jobs, snapshots, copies, status, notifications] = await Promise.all(['/servers','/keys','/backups','/audit','/engines','/jobs','/snapshots','/database-backups','/status','/notifications'].map(p => api(p)));
  Object.assign(state, {servers, keys, backups, audit, engines, jobs, snapshots, copies, status, notifications});
  render();
}

function input(name, label, value = '', type = 'text', attrs = '') {
  return `<label for="field-${name}">${label}</label><input id="field-${name}" name="${name}" type="${type}" value="${esc(value)}" ${attrs}>`;
}
function textarea(name, label, value = '', attrs = '') {
  return `<label for="field-${name}">${label}</label><textarea id="field-${name}" name="${name}" ${attrs}>${esc(value)}</textarea>`;
}
function select(name, label, values, selected) {
  return `<label for="field-${name}">${label}</label><select id="field-${name}" name="${name}" required><option value="">Choisir…</option>${values.map(v => `<option value="${v.id}" ${v.id === selected ? 'selected' : ''}>${esc(v.name)}</option>`).join('')}</select>`;
}

function openEditor(kind, id) {
  const collections = {server:state.servers, key:state.keys, backup:state.backups};
  const item = collections[kind].find(x => x.id === id) || {};
  if (kind === 'backup' && (!state.servers.length || !state.keys.length)) {
    notice('Ajoutez d’abord au moins un serveur et importez une clé SSH.');
    changeView(!state.servers.length ? 'servers' : 'keys');
    return;
  }
  editing = {kind, id};
  $('#editor-title').textContent = kind === 'key' ? 'Importer une clé SSH' : `${id ? 'Modifier' : 'Ajouter'} ${kind === 'server' ? 'un serveur' : 'une sauvegarde'}`;
  let html = input('name', 'Nom', item.name, 'text', 'required maxlength="100"');
  if (kind === 'server') {
    html += `<div class="row">${input('host','Adresse DNS ou IP',item.host,'text','required placeholder="serveur.local"')}${input('port','Port SSH',item.port || 22,'number','required min="1" max="65535"')}</div><small class="help">Le login SSH et les dossiers sont définis dans chaque sauvegarde.</small>`;
  } else if (kind === 'key') {
    html += input('file','Fichier de clé privée','','file') + '<small class="help">Sélectionnez un fichier ou collez son contenu ci-dessous.</small>';
    html += textarea('private_key','Clé privée','', 'spellcheck="false" autocomplete="off" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"');
    html += input('passphrase','Phrase secrète (si nécessaire)','','password','autocomplete="new-password"');
    html += '<small class="help">La clé est validée puis chiffrée. La phrase secrète sert uniquement au déverrouillage à l’import et n’est pas conservée.</small>';
  } else {
    html += `<div class="row">${select('server_id','Serveur',state.servers,item.server_id)}${input('login','Login SSH',item.login,'text','required placeholder="backup"')}</div>`;
    html += select('key_id','Clé SSH attachée',state.keys,item.key_id);
    html += select('engine','Moteur de sauvegarde',state.engines.map(e => ({id:e.id,name:e.name})),item.engine || 'rsnapshot');
    html += textarea('folders','Dossiers sources · un chemin absolu par ligne',(item.folders || []).join('\n'),'required placeholder="/srv/documents&#10;/etc/mon-application"');
    html += textarea('excludes','Exclusions · un motif par ligne (facultatif)',(item.excludes || []).join('\n'),'placeholder="*.tmp"');
    html += `<div class="row">${input('schedule','Heure quotidienne',item.schedule || '02:00','time','required')}${input('timezone','Fuseau horaire',item.timezone || 'Europe/Paris','text','required')}</div>`;
    html += '<label>Versions conservées</label><div class="row three">' + input('daily','Quotidiennes',item.retention?.daily ?? 7,'number','required min="1" max="3650"') + input('weekly','Hebdomadaires',item.retention?.weekly ?? 4,'number','required min="0" max="520"') + input('monthly','Mensuelles',item.retention?.monthly ?? 12,'number','required min="0" max="120"') + '</div>';
    html += input('timeout_seconds','Délai maximal (secondes)',item.timeout_seconds || 21600,'number','required min="60" max="86400"');
    html += '<small class="help">La sauvegarde se lance à l’heure choisie. Si l’heure est déjà passée, une occurrence est rattrapée aujourd’hui après validation de l’empreinte SSH.</small>';
  }
  if (kind !== 'key') html += `<label><input name="enabled" type="checkbox" ${item.enabled !== false ? 'checked' : ''}> ${kind === 'server' ? 'Serveur activé' : 'Configuration activée'}</label>`;
  $('#editor-fields').innerHTML = html;
  $('#form-error').hidden = true;
  $('#save').disabled = false;
  $('#save').textContent = kind === 'key' ? 'Importer la clé' : 'Enregistrer';
  $('#editor').showModal();
}

function changeView(name) {
  document.querySelectorAll('.view').forEach(v => {v.hidden = v.id !== name;});
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('active', b.dataset.view === name));
  $('#breadcrumb').textContent = ({overview:'Vue d’ensemble',backups:'Sauvegardes',servers:'Serveurs',keys:'Clés SSH',audit:'Activité',jobs:'Exécutions',snapshots:'Versions',database:'Protection SQLite',notifications:'Notifications'})[name];
}

function clearEditor() {
  $('#editor').close();
  $('#editor-fields').replaceChildren();
  editing = null;
}
$('#editor').addEventListener('cancel', clearEditor);

document.addEventListener('click', async event => {
  const button = event.target.closest('button');
  if (!button) return;
  try {
    if (button.dataset.verify) {
      await api(`/snapshots/${button.dataset.verify}/verify`,{method:'POST'});
      notice('Contrôle SHA256 ajouté à la file. La première exécution crée une référence.',true);changeView('jobs');await refresh();
    }
    if (button.dataset.restoreSnapshot) {
      restoreState={snapshot:button.dataset.restoreSnapshot,path:button.dataset.restorePath};
      $('#restore-form').reset();$('#restore-source').textContent=TardisPaths.navigation(state.snapshots.find(s => s.id===restoreState.snapshot),restoreState.path).label;
      $('#restore-status').textContent='Choisissez une destination sur le serveur. Son dossier parent doit déjà exister.';
      $('#restore-confirm').hidden=true;$('#restore-dialog').showModal();
    }
    if (button.dataset.run || button.dataset.test) {
      const id = button.dataset.run || button.dataset.test;
      const kind = button.dataset.run ? 'run' : 'test';
      await api(`/backups/${id}/${kind}`,{method:'POST',body:JSON.stringify({idempotency_key:crypto.randomUUID()})});
      notice('Opération ajoutée à la file.',true); changeView('jobs'); await refresh();
    }
    if (button.dataset.approveHost) {
      button.disabled = true;
      const keys = await api(`/servers/${button.dataset.approveHost}/host-key`);
      const key = keys.find(k => k.public_key.startsWith('ssh-ed25519 ')) || keys[0];
      if (await askConfirmation('Approuver cette clé d’hôte SSH ?', `Vérifiez cette empreinte auprès du serveur par un autre moyen : ${key.fingerprint}. L’approbation active la planification des sauvegardes de ce serveur.`, 'Approuver')) {
        await api(`/servers/${button.dataset.approveHost}/host-key`,{method:'POST',body:JSON.stringify(key)});
        notice('Clé d’hôte approuvée.',true); await refresh();
      }
      button.disabled = false;
    }
    if (button.dataset.log) {
      const job = await api(`/jobs/${button.dataset.log}`);
      $('#log-content').textContent = job.log || 'Opération en attente.';
      $('#log-dialog').showModal();
    }
    if (button.dataset.cancelJob) {await api(`/jobs/${button.dataset.cancelJob}/cancel`,{method:'POST'}); await refresh();}
    if (button.hasAttribute('data-database-copy')) {await api('/database-backups',{method:'POST'}); notice('Copie SQLite ajoutée à la file.',true); await refresh();}
    if (button.dataset.browse) await browseSnapshot(button.dataset.browse,button.dataset.path || '');
    if (button.dataset.view) changeView(button.dataset.view);
    if (button.dataset.new) openEditor(button.dataset.new);
    if (button.dataset.edit) openEditor(button.dataset.edit,button.dataset.id);
    if (button.hasAttribute('data-close')) clearEditor();
    if (button.dataset.copy) {
      const key = state.keys.find(k => k.id === button.dataset.copy);
      try {await navigator.clipboard.writeText(key.public_key); notice('Clé publique copiée.',true);}
      catch {notice('Copie indisponible : sélectionnez le texte de la clé publique.');}
    }
    if (button.dataset.delete) {
      const kind = button.dataset.delete;
      const item = (kind === 'server' ? state.servers : state.keys).find(x => x.id === button.dataset.id);
      $('#confirm-text').textContent = `Supprimer « ${item.name} » ? Un élément attaché à une sauvegarde ne peut pas être supprimé.`;
      $('#confirm-title').textContent = 'Supprimer cet élément ?';
      $('#confirmation button[value="confirm"]').textContent = 'Supprimer';
      $('#confirmation').returnValue = '';
      $('#confirmation').showModal();
      $('#confirmation').addEventListener('close',async function handle() {
        $('#confirmation').removeEventListener('close',handle);
        if ($('#confirmation').returnValue !== 'confirm') return;
        try {await api(`/${kind === 'server' ? 'servers' : 'keys'}/${item.id}`,{method:'DELETE'}); await refresh(); notice('Élément supprimé.',true);}
        catch (error) {notice(error.message);}
      });
    }
  } catch(error) {notice(error.message); button.disabled = false;}
});

$('#editor-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!editing) return;
  const form = new FormData(event.currentTarget);
  const {kind,id} = editing;
  const button = $('#save');
  button.disabled = true;
  $('#form-error').hidden = true;
  try {
    let payload;
    let path = kind === 'server' ? '/servers' : kind === 'key' ? '/keys' : '/backups';
    if (kind === 'key') {
      const file = form.get('file');
      if (file?.size && String(form.get('private_key')).trim()) throw new Error('Choisissez un fichier ou un collage, pas les deux.');
      if (file?.size) {
        if (file.size > 65536) throw new Error('Le fichier ne doit pas dépasser 64 Kio.');
        payload = new FormData();
        payload.set('name',form.get('name'));
        payload.set('file',file);
        payload.set('passphrase',form.get('passphrase'));
        path += '/import';
      } else {
        payload = {name:form.get('name'),private_key:form.get('private_key'),passphrase:form.get('passphrase')};
        if (!payload.private_key.trim()) throw new Error('Sélectionnez un fichier ou collez une clé privée.');
      }
    } else if (kind === 'server') {
      payload = {name:form.get('name'),host:form.get('host'),port:Number(form.get('port')),enabled:form.has('enabled')};
    } else {
      const lines = key => String(form.get(key)).split(/\r?\n/).map(x => x.trim()).filter(Boolean);
      payload = {name:form.get('name'),server_id:form.get('server_id'),key_id:form.get('key_id'),login:form.get('login'),engine:form.get('engine'),timeout_seconds:Number(form.get('timeout_seconds')),folders:lines('folders'),excludes:lines('excludes'),schedule:form.get('schedule'),timezone:form.get('timezone'),enabled:form.has('enabled'),retention:{daily:Number(form.get('daily')),weekly:Number(form.get('weekly')),monthly:Number(form.get('monthly'))}};
    }
    await api(path + (id ? `/${id}` : ''),{method:id ? 'PUT' : 'POST',body:payload instanceof FormData ? payload : JSON.stringify(payload)});
    clearEditor();
    notice(kind === 'key' ? 'Clé importée et chiffrée.' : 'Configuration enregistrée.',true);
    await refresh();
  } catch(error) {$('#form-error').textContent = error.message; $('#form-error').hidden = false;}
  finally {button.disabled = false;}
});

refresh().catch(error => notice(`Chargement impossible : ${error.message}`));
setInterval(() => refresh().catch(() => {}),5000);

function askConfirmation(title,text,label) {
  $('#confirm-title').textContent = title;
  $('#confirm-text').textContent = text;
  $('#confirmation button[value="confirm"]').textContent = label;
  $('#confirmation').returnValue = '';
  $('#confirmation').showModal();
  return new Promise(resolve => $('#confirmation').addEventListener('close',() => resolve($('#confirmation').returnValue === 'confirm'),{once:true}));
}

async function browseSnapshot(id,path) {
  const files = await api(`/snapshots/${id}/files?path=${encodeURIComponent(path)}`);
  const snapshot = state.snapshots.find(s => s.id === id);
  const navigation = TardisPaths.navigation(snapshot,path);
  const entries = files.map(file => TardisPaths.entry(snapshot,path,file));
  $('#browser-path').textContent = navigation.label;
  $('#browser-files').innerHTML = (navigation.parent === null ? '' : `<button data-browse="${id}" data-path="${esc(navigation.parent)}">↑ ${navigation.parent ? 'Dossier parent' : 'Dossiers sauvegardés'}</button>`) + entries.map(f => `<div class="audit-row"><span>${esc(f.name)} ${f.symlink ? '(lien non suivi)' : ''}</span><div class="card-actions">${f.directory ? `<button data-browse="${id}" data-path="${esc(f.path)}">Ouvrir</button>` : !f.symlink ? `<a href="${prefix}/api/v1/snapshots/${id}/download?path=${encodeURIComponent(f.path)}">Télécharger ↓</a>` : ''}${!f.symlink ? `<button data-restore-snapshot="${id}" data-restore-path="${esc(f.path)}">Restaurer…</button>` : ''}</div></div>`).join('');
  if (!$('#browser-dialog').open) $('#browser-dialog').showModal();
}

$('#restore-dialog').addEventListener('close',() => {restoreState=null;});
$('#restore-form').addEventListener('input',() => {
  if(restoreState?.preview && !restoreState.confirmed){restoreState.preview=null;$('#restore-confirm').hidden=true;$('#restore-status').textContent='Paramètres modifiés : préparer un nouvel aperçu.';}
});
$('#restore-form').addEventListener('submit',async event => {
  event.preventDefault();if(!restoreState)return;
  const form=new FormData(event.currentTarget);
  try {
    const job=await api(`/snapshots/${restoreState.snapshot}/restore/preview`,{method:'POST',body:JSON.stringify({path:restoreState.path,destination:form.get('destination'),overwrite:form.has('overwrite')})});
    restoreState.preview=job.id;restoreState.confirmed=null;$('#restore-confirm').hidden=true;$('#restore-status').textContent='Aperçu en attente…';await refresh();
  } catch(error){$('#restore-status').textContent=error.message;}
});
$('#restore-confirm').addEventListener('click',async () => {
  if(!restoreState?.preview)return;
  try {
    const job=await api(`/restores/${restoreState.preview}/confirm`,{method:'POST'});
    restoreState.confirmed=job.id;$('#restore-confirm').disabled=true;await refresh();
  } catch(error){$('#restore-status').textContent=error.message;}
});
function renderRestore() {
  if(!restoreState?.preview)return;
  const job=state.jobs.find(j => j.id===(restoreState.confirmed || restoreState.preview));
  if(!job)return;
  const result=job.result || {};
  if(restoreState.confirmed) {
    $('#restore-status').textContent=job.state==='succeeded' ? `Restauration terminée : ${result.destination}` : `${jobState(job.state)}${job.error ? ' : '+job.error : ''}`;
    $('#restore-confirm').hidden=true;return;
  }
  $('#restore-confirm').hidden=job.state!=='succeeded';$('#restore-confirm').disabled=false;
  $('#restore-status').textContent=job.state==='succeeded' ? `Destination : ${result.login}@${result.server}:${result.destination}\n${result.files} fichier(s) · ${bytes(result.bytes)}\n${result.overwrite ? 'ÉCRASEMENT AUTORISÉ des fichiers concernés.' : 'Aucun écrasement : nouvelle destination uniquement.'}\nAperçu valable 15 minutes.\n\n${result.preview.join('\n')}${result.truncated ? '\n…' : ''}` : `${jobState(job.state)}${job.error ? ' : '+job.error : ''}`;
}
