/* Presentation of immutable snapshot source paths; disk layout stays private. */
(function (root) {
  'use strict';
  function sourceRoot(snapshot, index) {
    const source = snapshot?.folders?.[index];
    if (snapshot?.engine !== 'rsnapshot' || typeof source !== 'string') return null;
    const suffix = source.replace(/^\/+|\/+$/g, '');
    return {source, path:`folder-${index}${suffix ? '/' + suffix : ''}`};
  }

  function entry(snapshot, currentPath, file) {
    const match = /^folder-(\d+)$/.exec(file.name);
    const source = !currentPath && file.directory && match ? sourceRoot(snapshot, Number(match[1])) : null;
    return source ? {...file, name:source.source, path:source.path} : file;
  }

  function navigation(snapshot, path) {
    if (!path) return {label:'Dossiers sauvegardés', parent:null};
    const match = /^folder-(\d+)(?:\/|$)/.exec(path);
    const source = match ? sourceRoot(snapshot, Number(match[1])) : null;
    if (source && (path === source.path || path.startsWith(source.path + '/'))) {
      const relative = path.slice(source.path.length);
      const label = source.source.replace(/\/$/, '') + relative || '/';
      return {label, parent:path === source.path ? '' : path.slice(0,path.lastIndexOf('/'))};
    }
    return {label:path, parent:path.split('/').slice(0,-1).join('/')};
  }

  const helpers = {entry, navigation};
  if (typeof module !== 'undefined' && module.exports) module.exports = helpers;
  else root.TardisPaths = helpers;
})(globalThis);
