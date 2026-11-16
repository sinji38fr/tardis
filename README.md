# TARDIS

Sauvegardes centralisées de serveurs Linux, avec interface Web en français et API REST.

[GitHub](https://github.com/sinji38fr/tardis) · [Docker Hub](https://hub.docker.com/r/shinji38fr/tardis) · [Exploitation](OPERATIONS.md)

## Installation

Docker Engine et Docker Compose sont nécessaires. Les images sont prévues pour Linux amd64 et arm64.

```sh
git clone --branch v1.0.0 https://github.com/sinji38fr/tardis.git
cd tardis
cp .env.example .env
docker compose pull
docker compose up -d
```

Ouvrir **http://localhost:8088**. L’interface s’ouvre directement, sans fenêtre de connexion.

Pour accéder à l’installation depuis un autre poste, modifier `.env` avant le démarrage :

```dotenv
TARDIS_PUBLIC_URL=http://adresse-du-serveur:8088
TARDIS_BIND_ADDRESS=0.0.0.0
TARDIS_PORT=8088
TARDIS_IMAGE=shinji38fr/tardis:1.0.0
```

Un sous-chemin fonctionne aussi, par exemple `https://outils.example/tardis`. Le reverse proxy doit transmettre le préfixe `/tardis` complet, sans le retirer. Le DNS, le certificat et le proxy sont configurés séparément. Le port interne reste 8000.

L’application n’intègre pas d’authentification : les personnes qui peuvent joindre l’instance peuvent l’administrer. Le port est limité à `127.0.0.1` par défaut.

## Fonctionnalités

- Ajout manuel des serveurs et validation de leur clé d’hôte SSH.
- Une sauvegarde = serveur + login + clé SSH + liste de dossiers.
- Import de clés existantes, avec ou sans phrase secrète ; réutilisation entre sauvegardes.
- Coffre privé isolé du Web, avec migration automatique des clés des versions précédentes.
- Moteurs interchangeables : rsnapshot disponible, basé sur rsync et les liens physiques.
- Sauvegardes manuelles et planifiées, rétention, historique et journaux persistants.
- Exploration des versions avec les vrais chemins sources, téléchargement de fichiers.
- Restauration sur le serveur avec aperçu et confirmation ; aucun écrasement par défaut.
- Références SHA256 et vérification de contenu à la demande.
- Statistiques physiques tenant compte des fichiers partagés par liens physiques.
- Copies SQLite cohérentes au démarrage puis chaque heure, intégrité vérifiée et rétention.
- Webhooks avec signature HMAC facultative, file persistante et réessais.
- API documentée dans `/docs` et schéma `/openapi.json`.

## Première sauvegarde

1. Ajouter le serveur et son port SSH dans **Serveurs**.
2. Lire puis approuver son empreinte après vérification auprès du serveur.
3. Importer une clé privée dans **Clés SSH**. La clé publique dérivée est disponible pour la copier dans `authorized_keys` sur le serveur.
4. Créer une sauvegarde : serveur, login (y compris `root` si autorisé par le serveur), clé et dossiers absolus.
5. Tester, puis lancer la sauvegarde ou attendre son horaire.
6. Dans **Versions**, explorer les fichiers, les télécharger ou demander une restauration.

Le serveur distant nécessite SSH et rsync. Le login doit pouvoir lire les dossiers. TARDIS n’installe pas d’agent et ne modifie pas les règles SSH du serveur.

## Protection de SQLite

Une copie est créée au démarrage puis toutes les heures, indépendamment des transferts. Chaque copie est vérifiée et accompagnée d’un SHA256. Les 30 dernières sont conservées par défaut.

```dotenv
TARDIS_DATABASE_BACKUP_INTERVAL=3600
TARDIS_DATABASE_BACKUP_KEEP=30
```

Les copies sont téléchargeables dans **Protection SQLite**. Conserver également le volume de secrets pour pouvoir déchiffrer les clés après une reprise. Voir [OPERATIONS.md](OPERATIONS.md).

## Notifications

Réglages facultatifs dans `.env` :

```dotenv
TARDIS_WEBHOOK_URL=https://votre-service.example/evenements
TARDIS_WEBHOOK_SECRET=un-secret-partage
```

Le secret permet au destinataire de vérifier la signature HMAC SHA256. Le corps JSON contient le type d’événement, son identifiant, la date et un résumé du résultat ; il ne contient pas les fichiers sauvegardés ni les clés privées. Le statut de livraison est visible dans **Notifications**.

## Mise à jour et développement

Les données sont dans des volumes Docker persistants. Ne pas utiliser `docker compose down -v` pour mettre à jour.

```sh
docker compose pull
docker compose up -d
```

Pour construire depuis les sources :

```sh
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

Tests API et intégration SSH dans un conteneur jetable :

```sh
docker build --target test -t tardis-test .
docker run --rm tardis-test
node --test tests/snapshot-paths.test.cjs
```

Les tests ne contactent aucun serveur de votre infrastructure. La CI GitHub exécute ces vérifications à chaque publication de code.

Code TARDIS sous licence MIT. Les outils et bibliothèques de l’image conservent leurs licences respectives : voir [NOTICE](NOTICE).
