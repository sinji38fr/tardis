const {test} = require('node:test');
const assert = require('node:assert/strict');
const paths = require('../app/static/snapshot-paths.js');
const snapshot = {engine:'rsnapshot',folders:['/srv/photos','/home/alice/photos','/']};

test('afficher les chemins complets et distinguer les dossiers de même nom', () => {
  assert.deepEqual(paths.entry(snapshot,'',{name:'folder-0',directory:true,path:'folder-0'}),
    {name:'/srv/photos',directory:true,path:'folder-0/srv/photos'});
  assert.equal(paths.entry(snapshot,'',{name:'folder-1',directory:true}).name,'/home/alice/photos');
});
test('entrer directement dans le dossier source et revenir à la liste', () => {
  assert.deepEqual(paths.navigation(snapshot,'folder-0/srv/photos'),{label:'/srv/photos',parent:''});
  assert.deepEqual(paths.navigation(snapshot,''),{label:'Dossiers sauvegardés',parent:null});
});
test('conserver une navigation lisible dans les sous-dossiers', () => {
  assert.deepEqual(paths.navigation(snapshot,'folder-1/home/alice/photos/2026/vacances'),
    {label:'/home/alice/photos/2026/vacances',parent:'folder-1/home/alice/photos/2026'});
});
test('gérer la sauvegarde de la racine du serveur', () => {
  assert.deepEqual(paths.entry(snapshot,'',{name:'folder-2',directory:true}),{name:'/',directory:true,path:'folder-2'});
  assert.deepEqual(paths.navigation(snapshot,'folder-2/etc'),{label:'/etc',parent:'folder-2'});
});
test('ne pas renommer les fichiers ni les dossiers utilisateur internes', () => {
  const file={name:'folder-0',path:'folder-0/srv/photos/folder-0',directory:true};
  assert.equal(paths.entry(snapshot,'folder-0/srv/photos',file),file);
});
test('respecter les autres moteurs et les anciennes métadonnées absentes', () => {
  const file={name:'folder-0',path:'folder-0',directory:true};
  assert.equal(paths.entry({engine:'autre',folders:['/data']},'',file),file);
  assert.equal(paths.entry(undefined,'',file),file);
});
