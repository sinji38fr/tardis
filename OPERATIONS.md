# TARDIS 1.0 — Exploitation

## Services et persistance

Le service ponctuel `initialize` prépare les volumes et migre le schéma et les anciennes clés. Le Web démarre ensuite ; le worker assure les opérations longues et les tâches périodiques.

| Volume | Contenu | Accès Web | Accès worker |
| --- | --- | --- | --- |
| `tardis_data` | SQLite, configurations, tâches, audit | lecture/écriture | lecture/écriture |
| `tardis_secrets` | Ancienne clé maître et clé privée d’enveloppe | aucun montage | lecture seule |
| `tardis_public_keys` | Clé publique d’import du coffre | lecture seule | lecture seule |
| `tardis_backups` | Versions, caches et références SHA256 | lecture seule | lecture/écriture |
| `tardis_database_backups` | Copies SQLite vérifiées | lecture/écriture pour la migration | lecture/écriture |

Le conteneur d’initialisation dispose des droits d’écriture nécessaires dans les volumes de clés puis se termine. Les services utilisent l’UID 10001, sans privilèges supplémentaires. Le worker matérialise une clé SSH dans son `/tmp` en mémoire pendant une opération puis la supprime. Le Web peut valider une clé nouvellement importée et la chiffrer avec la clé publique ; il ne possède ni la clé maître ni la clé privée de déchiffrement.

Les anciennes clés Fernet sont migrées vers des enveloppes RSA-OAEP/SHA256 + Fernet, sans changer leur identité SSH. Une clé privée de coffre manquante n’est jamais remplacée silencieusement si la base contient déjà des clés au nouveau format. Conserver **tout le volume `tardis_secrets`**, pas seulement `master.key`.

## Copies SQLite et reprise

Copie cohérente via SQLite Online Backup, compatible WAL. Chaque fichier provisoire passe `PRAGMA integrity_check`, reçoit un SHA256 puis est publié par renommage. Une nouvelle copie vérifiée précède la suppression des anciennes. Une copie est aussi faite avant une migration de schéma. La tâche périodique fonctionne indépendamment des transferts distants.

Les réglages `TARDIS_DATABASE_BACKUP_INTERVAL` et `TARDIS_DATABASE_BACKUP_KEEP` valent respectivement 3600 secondes et 30 copies par défaut. L’intervalle est vérifié chaque minute et ne peut pas être inférieur à 60 secondes. Les fichiers temporaires ne sont pas proposés au téléchargement.

Un volume distinct ne protège pas contre la perte de l’hôte : copier les volumes de données, de copies et de secrets sur un autre support selon votre politique de reprise.

Pour reprendre une base :

1. Arrêter Web et worker : `docker compose stop web worker`.
2. Choisir une copie, vérifier son SHA256 et son `PRAGMA integrity_check`.
3. Conserver les fichiers actifs `tardis.db`, `tardis.db-wal` et `tardis.db-shm` avant intervention.
4. Remplacer `/data/tardis.db` par la copie et retirer les anciens fichiers WAL/SHM du répertoire actif après leur conservation. Ils ne doivent pas être rejoués sur la copie restaurée.
5. Restaurer le volume de secrets correspondant, avec son propriétaire UID 10001.
6. Relancer `docker compose up -d`, vérifier l’état puis tester une connexion SSH.

Le volume des versions doit être repris avec la base si l’hôte est reconstruit. Une référence SQL ne recrée pas les fichiers absents. Aucune restauration de la base active n’est déclenchée depuis l’interface.

## Collecte, planification et rétention

La configuration est figée à la mise en file. Une seule opération est autorisée à la fois par sauvegarde ; le worker traite les collectes en série. Après un redémarrage, une occurrence quotidienne manquée est rattrapée. Une clé d’idempotence basée sur la date locale évite les doublons lors des changements d’heure. Les tâches interrompues sont marquées en échec ; une version complètement publiée avant l’interruption peut être finalisée dans le catalogue.

rsnapshot effectue `sync` puis sa rotation de cache après succès. TARDIS publie des arborescences stables identifiées par UUID. Les données inchangées sont partagées par liens physiques ; rsync n’utilise pas `--inplace`. Un transfert incomplet ne publie aucune version et ne supprime pas la dernière version utilisable.

Rétention commune aux moteurs : les N dernières versions, plus la dernière version de chacune des W dernières semaines et des M derniers mois observés. Valeurs initiales : 7 / 4 / 12. Une exécution manuelle compte comme une version. Les versions communes à plusieurs niveaux ne sont pas dupliquées.

Un téléchargement ou une restauration tient un verrou de lecture ; les versions concernées ne sont pas supprimées pendant l’opération. Un aperçu de restauration réussi les protège aussi pendant 15 minutes. Le nettoyage reprend après les opérations si nécessaire.

Les caches des anciennes révisions sont purgés automatiquement, sauf ceux référencés par une tâche en attente ou en cours et celui de la configuration actuelle. Effacer un cache ne modifie pas les fichiers des versions conservées.

## Statistiques et espace disque

La mesure additionne les blocs alloués une seule fois par paire périphérique/inode dans l’ensemble des versions et caches. La taille logique additionne les fichiers visibles et peut donc être supérieure à l’occupation physique. Les métadonnées de répertoires sont incluses dans l’estimation physique. Cette mesure vise les liens physiques ; elle ne calcule pas les économies de compression ou de déduplication propres au système de fichiers.

La mesure est actualisée périodiquement et après les collectes. L’occupation globale du système de fichiers inclut aussi les données extérieures à TARDIS. Un avertissement est émis à partir de 80 % ; à 95 %, une nouvelle collecte est refusée sans effacement d’urgence.

## Restauration distante

Dans l’explorateur, sélectionner **Restaurer…**, renseigner une destination absolue sur le serveur et préparer l’aperçu. Le dossier parent doit déjà exister. Le serveur, le login, la destination, le nombre de fichiers et le volume sont affichés avant la confirmation.

Par défaut, la destination doit être nouvelle. Les données sont transférées vers un nom temporaire unique sur le serveur puis publiées sans écrasement. Si la destination apparaît entre-temps, la publication échoue et conserve le contenu existant. Un échec peut laisser un dossier distant `.tardis-restore-<id>` récupérable par l’administrateur.

L’écrasement nécessite de cocher explicitement l’option puis de confirmer l’aperçu. Une empreinte du contenu distant et un nouveau dry-run sont comparés juste avant le transfert ; une différence impose un nouvel aperçu. Cette vérification ne verrouille pas les applications qui écrivent sur le serveur pendant le transfert : la restauration vers un nouveau dossier reste préférable pour des données actives. Les fichiers supplémentaires de la destination ne sont jamais supprimés automatiquement.

Les aperçus expirent après 15 minutes et leur confirmation est idempotente. Les clés d’hôte restent vérifiées strictement. Les liens symboliques sélectionnés directement ne sont pas suivis ; la restauration de dossiers utilise `--safe-links` pour écarter les liens hors de l’arborescence. Il ne s’agit pas d’une restauration bare-metal.

## Contrôles SHA256

La première demande crée une **référence de contenu** de la version actuelle ; elle ne prouve pas à elle seule que les fichiers correspondent à la source distante initiale. Les demandes suivantes recalculent les empreintes et signalent les fichiers modifiés, ajoutés ou absents. La référence est séparée de l’arborescence partagée par liens physiques et ne peut être écrite par le Web.

Les fichiers ordinaires sont contrôlés ; les liens symboliques et fichiers spéciaux ne sont pas lus comme contenu. Un échec apparaît dans les exécutions et génère un événement de notification si le webhook est configuré.

## Webhooks

Configurer `TARDIS_WEBHOOK_URL` et, facultativement, `TARDIS_WEBHOOK_SECRET` dans `.env`. Le worker adresse des POST JSON pour les succès, échecs et alertes d’espace. L’en-tête `X-Tardis-Signature` vaut `sha256=<HMAC du corps brut>` lorsqu’un secret est configuré. `X-Tardis-Delivery` est l’identifiant stable de livraison : le destinataire peut le dédupliquer.

La file d’envoi est persistante dans SQLite. Les échecs sont réessayés jusqu’à cinq tentatives, avec temporisation croissante. Les redirections HTTP ne sont pas suivies. Un échec de notification ne transforme pas une sauvegarde réussie en échec. Les événements ne contiennent pas les clés ni les fichiers sauvegardés. Le secret n’est pas exposé par l’API Web.

## API et moteurs

Documentation interactive `/docs`, schéma `/openapi.json`, ressources sous `/api/v1`. Les mutations requièrent `X-Tardis-Request: 1` et les requêtes de navigateur d’une autre origine sont refusées. Cet en-tête n’est pas une authentification.

Routes complémentaires :

- `POST /snapshots/{id}/restore/preview` : `{path, destination, overwrite}` ; retourne une tâche.
- `POST /restores/{preview_id}/confirm` : confirme un aperçu réussi non expiré.
- `POST /snapshots/{id}/verify` : crée une référence ou vérifie le contenu.
- `GET /notifications` : état des livraisons et activation du webhook.

Un moteur implémente `name`, `label` et `capture(context, destination)` dans `app/engines`. Enregistrer l’adaptateur dans `default_registry()` : il apparaît dans le sélecteur et `/engines`. `engine_options` contient ses options non secrètes ; leur validation appartient à l’adaptateur. Le contexte fournit SSH, journal, délai et point de contrôle d’annulation. `cleanup(work_dir, keep_revisions)` permet de purger son cache.

L’ordonnanceur, la file, les UUID, les téléchargements et la rétention restent indépendants du moteur. Le contrat actuel produit des fichiers locaux à partir de sources SSH. Un dépôt opaque ou objet nécessite une capacité de restauration propre avant d’être annoncé compatible.

Les bases distantes actives doivent être sauvegardées via leurs dumps ou outils dédiés ; une copie de fichiers ne garantit pas leur cohérence transactionnelle.
