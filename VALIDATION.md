# Vérification de la version 1.0

Vérifié le 16 novembre 2026 avec Docker Desktop (moteur Linux).

- Construction de l’image de test : réussie.
- Tests API, persistance et intégration : **32 tests**, avec suite SSH isolée et dépendances verrouillées.
- Navigation frontend : **6 tests réussis**.
- Vérification syntaxique JavaScript avec `node --check` : réussie.
- Validation de la configuration avec `docker compose config --quiet` : réussie.
- Construction et lancement du service réel avec Compose : réussis.
- Le contrôle de l’instance après mise à jour vérifie la version 1.0, le heartbeat du worker et la présence d’une copie SQLite d’intégrité `ok`.

Les tests couvrent les sauvegardes distinctes sur un même serveur, le partage d’une clé entre sauvegardes, les révisions, l’import par fichier d’une clé protégée par phrase secrète, le chiffrement, les erreurs sans secrets, les références protégées contre la suppression, les chemins invalides, la protection intersites et le routage sous `/tardis`.

Les tests de collecte utilisent un **vrai serveur SSH jetable sur 127.0.0.1 à l’intérieur du conteneur** et les binaires rsync/rsnapshot. Ils vérifient les chemins contenant des espaces, les exclusions, le partage des inodes des fichiers inchangés, la conservation du contenu précédent, le téléchargement d’un ancien fichier, le refus des traversées et liens symboliques, la non-publication d’un transfert incomplet et la rétention différée pendant un téléchargement. Une collecte supplémentaire est exécutée avec l’UID non privilégié 10001 utilisé en production.

Régression SSH : collecte réelle sur le port standard 22 avec une clé utilisateur RSA et une clé d’hôte ED25519. Le fichier known_hosts utilise le nom d’hôte sans crochets sur le port 22 et la forme `[hôte]:port` pour les ports non standards. Le contrôle strict reste activé.

Les copies SQLite sont testées pendant des écritures concurrentes, avec contrôle d’intégrité, rétention, téléchargement et relecture de leur contenu. Les tests vérifient également la tâche automatique indépendante, la copie avant migration 0.1, la reprise de file après interruption, la déduplication de planification, l’idempotence, l’annulation et le verrou de worker unique. Un second adaptateur de test confirme que la publication reste indépendante de rsnapshot.

La version 1.0 ajoute les tests du Web sans montage du coffre privé, la migration des anciennes clés, le refus de remplacer une clé privée de coffre manquante, les mesures physiques par inode, la purge des caches, les webhooks signés et leurs réessais. Les restaurations de fichiers et de dossiers sont exécutées via SSH après aperçu ; les tests contrôlent l’idempotence, le refus des collisions et le refus d’écrasement si la destination a changé après l’aperçu. Un test de checksum détecte une corruption volontaire.

La vérification visuelle dans un navigateur n’a pas pu être réalisée avec les outils disponibles. Aucun serveur réel de l’infrastructure utilisateur n’est contacté par les tests.

Reproduire les tests :

```sh
docker build --target test -t tardis-test:1.0.0 .
docker run --rm tardis-test:1.0.0
node --test tests/snapshot-paths.test.cjs
```
