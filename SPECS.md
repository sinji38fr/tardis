# TARDIS — Spécifications de conception

Spécifications fonctionnelles du projet TARDIS.

Ce document conserve les choix de conception initiaux. Pour la version 1.0 livrée, les fonctionnalités et modalités d’exploitation à jour figurent dans README.md, OPERATIONS.md et ROADMAP.md.

Ce document reprend les décisions de l’échange récupéré, puis les précisions utilisateur et les choix de conception du 16 novembre 2026. Les choix ci-dessous constituent la cible de la V1 ; l’avancement réel figure dans ROADMAP.md et les limites de la version 0.1 dans README.md.

Mise à jour 0.2 : la protection automatique de SQLite et les collectes réelles sont implémentées. Les décisions d’implémentation actualisées (rétention commune aux moteurs, worker en série, téléchargement de fichiers et limites restantes) figurent dans OPERATIONS.md, qui prévaut sur les choix prévisionnels ci-dessous.

- SQLite : copie cohérente au démarrage puis toutes les heures, rétention initiale de 30 copies, contrôle d’intégrité et volume distinct. Intervalle et rétention configurables.
- Une sauvegarde référence un moteur et des options JSON ; rsnapshot est le premier adaptateur. Catalogue, ordonnanceur et rétention sont indépendants des moteurs.

## Objectif et identité

TARDIS est un outil de sauvegarde centralisée des serveurs Linux, avec consultation des versions et restauration depuis une interface Web.

Nom développé proposé dans l’échange : **Time Archive & Recovery Data Integrity System**.
Publication sur GitHub prévue par l’utilisateur ; projet open source. Licence retenue : MIT pour le code TARDIS, avec conservation des licences des dépendances distribuées.

## Architecture validée

L’utilisateur a confirmé « ok on pars sur ça » à la fin de la discussion.

- Hébergement de TARDIS dans Docker sur **Tochiro**.
- **Tochiro pilote l’ensemble** : les serveurs distants n’orchestrent pas les sauvegardes.
- Collecte des fichiers distants par **SSH**, en mode récupération depuis Tochiro.
- Moteur de sauvegarde : **rsnapshot + rsync**.
- Sauvegardes incrémentales utilisant les **liens physiques (hard links)** pour partager les fichiers inchangés entre versions.
- Snapshots versionnés avec **politique de rétention configurable**.
- **FastAPI** pour exposer une API REST, notamment à destination d’**Arcadia**.
- **Interface Web** pour configurer, surveiller et restaurer les sauvegardes.
- Fonctionnement **sans agent TARDIS** sur les serveurs distants.

## Serveurs distants

Les serveurs sont ajoutés **manuellement**, sans découverte automatique ni serveur préconfiguré. Lacia et Kooka étaient uniquement des exemples de discussion.

Un serveur représente une destination SSH (nom, adresse DNS/IP, port). Une **configuration de sauvegarde** associe **un serveur + un login SSH + une liste de dossiers**. Plusieurs configurations peuvent viser le même serveur, avec des logins et des dossiers différents. Le login et les dossiers appartiennent à la configuration de sauvegarde, pas au serveur.

Prérequis explicités :

- Serveur SSH et programme rsync disponibles.
- Clé SSH dédiée autorisée pour les connexions automatiques depuis Tochiro.
- Compte SSH disposant des droits de lecture sur les données sauvegardées.
- Permissions adaptées pour les fichiers protégés ; un sudo rsync contrôlé a été évoqué, sans configuration arrêtée.
- Aucun besoin de rsnapshot, de Docker ou d’agent TARDIS sur les machines sources.

## Stockage et restauration

- Stockage des versions sur Tochiro.
- Système de fichiers compatible avec les liens physiques, ext4 ou XFS cités comme exemples.
- Les fichiers liés physiquement doivent rester sur le même système de fichiers.
- Chaque version présente une arborescence complète consultable, sans reconstruction d’une chaîne d’incréments.
- Restauration d’un fichier par copie depuis la version choisie.
- Les contenus des snapshots ne doivent pas être modifiés directement : une modification en place d’un inode partagé affecterait plusieurs versions.
- Un gros fichier modifié peut occuper une nouvelle copie complète ; le partage par hard links concerne les fichiers inchangés.

## Modèle fonctionnel V1

### Serveur

- Identifiant stable UUID, nom d’affichage unique, adresse DNS/IP, port SSH (22 par défaut), activation.
- Ajout, modification et suppression manuels depuis le Web ou l’API.
- Suppression refusée tant que des configurations de sauvegarde le référencent ; désactivation possible.
- Empreinte de clé d’hôte SSH enregistrée après validation explicite par l’administrateur. Tout changement bloque la connexion jusqu’à nouvelle validation.

### Configuration de sauvegarde

- Identifiant stable UUID et nom d’affichage unique.
- Un serveur référencé, un login SSH explicite, une référence de clé privée SSH.
- Une liste non vide de dossiers sources absolus ; doublons et chemins imbriqués refusés pour éviter les copies redondantes.
- Clé SSH choisie dans la bibliothèque de clés importées, avec possibilité d’importer une clé pendant la création ou l’édition de la sauvegarde.
- Liste de motifs d’exclusion optionnels, affichée et testable avant exécution.
- Planification, fuseau horaire, politique de rétention, délai maximal et activation propres à cette configuration.
- Bouton de test : SSH, disponibilité de rsync et lisibilité de chaque dossier.
- Boutons de lancement immédiat, désactivation et consultation des exécutions.
- Un changement de serveur, login ou dossier crée une nouvelle révision de configuration. Chaque exécution conserve la révision exacte utilisée ; les versions anciennes restent identifiables et consultables.

Exemple illustratif : deux configurations sur un même serveur peuvent sauvegarder respectivement `/home/alice` avec le login `alice`, puis `/srv/app` et `/etc/app` avec le login `backup`. Aucune de ces valeurs n’est préchargée.

### Exécution et snapshot

- Une exécution appartient à une configuration ; elle a un UUID, un déclencheur (horaire, Web ou API), une révision de configuration, des dates et un journal.
- États : en attente, en cours, réussie, échouée, annulée. Les transferts incomplets et fichiers illisibles sont signalés comme échec.
- Un snapshot consultable n’est publié qu’après collecte réussie ; il conserve la date, le niveau de rétention et la configuration d’origine.
- L’interface distingue les identifiants de snapshots et les positions internes de rotation rsnapshot. Une position telle que `daily.0` n’est jamais utilisée comme identifiant public durable.
- L’historique affiche durée, résultat, volumes transférés lorsqu’ils sont disponibles et erreurs exploitables.

### Bibliothèque de clés SSH

- Import manuel d’une **clé privée SSH existante**, par fichier ou collage, depuis l’interface Web ; import également disponible par l’API avec droit administrateur.
- Chaque clé possède un UUID, un nom d’affichage, son type, son empreinte publique et sa date d’import. La clé publique correspondante est dérivée pour afficher l’empreinte et permettre sa copie vers les serveurs.
- Une sauvegarde référence une clé de cette bibliothèque ; une même clé peut être attachée à plusieurs sauvegardes, y compris sur des serveurs différents. L’attachement appartient à la sauvegarde, indépendamment du serveur et du login.
- Import validé avant enregistrement : format lisible et clé privée utilisable. Une clé publique seule ne permet pas l’authentification et est refusée comme clé de connexion.
- Prise en charge des clés privées protégées par une phrase secrète ; saisie à l’import, validation du déverrouillage et stockage chiffré pour permettre les exécutions planifiées sans intervention.
- La clé privée et son éventuelle phrase secrète sont chiffrées au repos avec une clé maître fournie au déploiement hors SQLite et hors dépôt. La procédure d’exploitation inclut la conservation séparée de cette clé maître pour la reprise après incident.
- Le Web transmet le secret à l’import et ne peut pas le relire ensuite ; seul le worker dispose de la clé maître et peut déchiffrer les secrets pour une opération SSH. Les fichiers temporaires éventuels sont réservés au worker, avec permissions strictes, puis supprimés après usage.
- Aucun affichage ni téléchargement ultérieur de clé privée ou de phrase secrète. Les vues et réponses API exposent uniquement les métadonnées et les sauvegardes qui utilisent la clé.
- Changer la clé attachée à une sauvegarde crée une nouvelle révision de configuration. Une exécution déjà en cours conserve sa référence initiale ; les prochaines utilisent la nouvelle clé.
- Suppression d’une clé refusée si elle est attachée à une sauvegarde ou utilisée par une exécution en cours. Les références historiques conservent uniquement les métadonnées utiles après suppression du secret.
- Importer ou attacher une clé ne modifie pas automatiquement les fichiers `authorized_keys` des serveurs : le test de connexion vérifie si son accès est déjà autorisé pour le login de la sauvegarde.

## Planification et rétention

- Réglages initiaux : collecte quotidienne à **02:00**, fuseau **Europe/Paris**, conservation de **7 versions quotidiennes, 4 hebdomadaires et 12 mensuelles**.
- Chaque configuration peut modifier ces valeurs et son horaire ; valeurs de rétention entières positives ou zéro pour désactiver un niveau optionnel.
- Niveaux hebdomadaire et mensuel : rotation/promotion rsnapshot ; les horaires et l’ordre sont calculés par l’ordonnanceur pour éviter une collecte distante supplémentaire pour chaque niveau.
- Une configuration dispose de son propre fichier rsnapshot, de sa racine de snapshots et de son verrou.
- Aucune exécution ni rotation concurrente sur une même configuration. Verrou exclusif également pour les lectures de restauration qui doivent rester cohérentes pendant une rotation.
- Au maximum **2 collectes simultanées** au démarrage ; plafond global configurable. Exécutions d’un même serveur sérialisées par défaut.
- Délai maximal initial : **6 heures**, configurable. Après redémarrage, les exécutions interrompues sont marquées en échec et une seule exécution horaire manquée est rattrapée.
- Les occurrences horaires répétées lors d’un changement d’heure sont dédupliquées ; une heure locale inexistante est reportée au premier instant valide.
- Un échec de collecte ne doit pas éliminer la dernière version complète utilisable. La stratégie de publication et de rotation devra être vérifiée avec le moteur avant livraison.

## Stockage

- Un volume dédié monté dans le conteneur sous `/backups`, configurable au déploiement.
- Racines distinctes par UUID de configuration ; les chemins sources sont conservés sous une arborescence explicite sans collision de noms.
- Configuration, base de données et clés SSH persistent dans des volumes séparés du stockage des snapshots.
- Seuils initiaux : avertissement à 80 % d’occupation, refus de nouvelle collecte à 95 %. Aucun effacement exceptionnel automatique pour libérer de la place.
- Espace affiché à deux niveaux : taille logique des versions et occupation physique globale. Ne pas additionner les tailles logiques pour annoncer une consommation physique, à cause des hard links.
- La capacité réelle et le chemin hôte seront renseignés au déploiement ; aucun disque existant n’est formaté ou sélectionné automatiquement.

## Interface Web

Interface en français, réalisée avec les templates Jinja2 de FastAPI, JavaScript natif et une feuille CSS locale, sans service frontend distinct.

Écrans :

1. Tableau de bord : dernières exécutions, échecs, prochaines sauvegardes et espace disque.
2. Serveurs : saisie manuelle, édition, état et empreinte SSH.
3. Sauvegardes : serveur, login, dossiers, exclusions, horaire, rétention et test de connexion.
4. Exécutions : progression, journaux, résultat et historique.
5. Versions et restauration : choix d’une configuration, d’une date, puis navigation dans les fichiers.
6. Paramètres : stockage, concurrence, notifications et accès API.
7. Clés SSH : import, métadonnées, empreinte, copie de la clé publique, sauvegardes associées et suppression des clés inutilisées.

## Restauration V1

- Téléchargement d’un fichier ou d’une archive tar de fichiers/dossiers sélectionnés depuis une version réussie.
- Restauration vers le serveur source possible avec un aperçu préalable et une destination absolue explicite.
- Par défaut, restauration dans un **nouveau dossier** et refus d’écraser un fichier existant. L’écrasement nécessite une confirmation explicite et un aperçu des fichiers concernés.
- Une restauration utilise le serveur et le login actuellement sélectionnés, affichés dans l’aperçu ; aucun retour distant n’est déclenché par la simple navigation dans une version.
- Les permissions distantes peuvent empêcher une restauration même si la sauvegarde est lisible : cette erreur est remontée clairement.
- Protection des chemins : confinement dans la version sélectionnée, refus des traversées `..` et absence de suivi des liens symboliques vers l’extérieur.
- Suppression d’une configuration : désactivation immédiate, snapshots conservés. Purge des données comme action séparée, avec confirmation et journalisation.

## API et intégration Arcadia

API JSON sous `/api/v1`, documentation OpenAPI générée par FastAPI.

- CRUD des serveurs et configurations de sauvegarde.
- Import, liste des métadonnées et suppression des clés SSH ; attachement par référence dans une configuration de sauvegarde. Aucun endpoint de lecture des secrets.
- Test de connexion, lancement d’une sauvegarde, lecture du statut et des journaux.
- Liste des snapshots, navigation et téléchargement.
- Prévisualisation puis déclenchement d’une restauration.
- État du service et stockage.
- Opérations longues asynchrones : réponse avec identifiant d’exécution et consultation ultérieure du statut.
- Clé d’idempotence prise en charge pour les lancements API, afin qu’un nouvel essai d’Arcadia ne crée pas de doublon.
- Jetons API révocables avec droits `read`, `backup:run`, `restore` ou `admin`. Arcadia reçoit au départ `read` et `backup:run`.
- Les noms exacts des routes et les schémas seront décrits dans OpenAPI lors de l’implémentation.

## Architecture applicative et accès

- Python + FastAPI ; SQLite pour les configurations, révisions, exécutions et métadonnées de snapshots.
- Deux services Docker Compose construits depuis la même image : application Web/API et worker/ordonnanceur unique.
- File persistante en SQLite ; prise des tâches transactionnelle, reprise après incident, verrous interprocessus pour rsnapshot. Aucun Redis nécessaire en V1.
- **Précision utilisateur : aucun écran de connexion.** L’interface est accessible directement, sans compte ni session utilisateur. Toute personne ayant accès à l’instance peut l’administrer.
- URL publique configurable par `TARDIS_PUBLIC_URL`, y compris un sous-chemin ; adresse d’écoute et port hôte configurables par `TARDIS_BIND_ADDRESS` et `TARDIS_PORT`. La configuration ne crée pas de DNS ni de certificat.
- Protection des mutations contre les soumissions intersites : en-tête spécifique obligatoire et vérification de l’origine du navigateur. Aucun CORS permissif.
- La version 0.1 applique également l’accès direct à l’API ; les jetons limités d’Arcadia décrits dans la cible restent une évolution, sans imposer un login à l’interface.
- Après import, secrets SSH déchiffrables uniquement par le worker, hors dépôt et hors journaux. Le Web accepte leur écriture mais les API ne permettent jamais leur relecture.
- Accès prévu sur le réseau local ; HTTPS au niveau du reverse proxy de déploiement. Aucun accès Internet public configuré automatiquement.
- Appels système avec arguments séparés, sans interpolation dans un shell ; validation des hôtes, logins et chemins.

## Cohérence des données et exploitation

- La V1 sauvegarde des **fichiers**. Une copie des fichiers d’une base active ne constitue pas automatiquement une sauvegarde cohérente de la base.
- Pour les bases : sauvegarder un dossier de dumps préparés par l’exploitation. L’orchestration de dumps et de hooks distants arbitraires reste hors V1.
- Journaux par exécution et journal d’audit des modifications, lancements, restaurations et purges ; secrets masqués.
- Notifications dans le tableau de bord ; webhook HTTP optionnel pour les échecs et alertes d’espace. Pas de service de notification imposé.
- Vérification automatique du résultat rsync, de l’existence des dossiers attendus et du succès de publication. Un contrôle de contenu par checksum peut être lancé manuellement ; coût et résultat affichés séparément.
- Tests de restauration nécessaires avant usage réel ; la sauvegarde de la base TARDIS et de sa configuration est incluse dans la procédure d’exploitation.

## Critères d’acceptation V1

1. Ajouter manuellement un serveur et créer deux configurations avec des logins et listes de dossiers distincts.
2. Tester l’accès, lancer les deux sauvegardes et consulter leurs résultats séparément.
3. Après modification d’un fichier source, obtenir deux versions restaurables ; vérifier le partage physique d’un fichier inchangé et la conservation de l’ancien contenu du fichier modifié.
4. Modifier une configuration sans perdre l’identification ni l’accès aux versions antérieures.
5. Télécharger puis restaurer un fichier dans un nouveau dossier ; vérifier son contenu et le refus d’un écrasement implicite.
6. Vérifier la rétention, les verrous, les reprises après redémarrage et l’absence de snapshot publié en cas de transfert incomplet.
7. Déclencher une sauvegarde par l’API avec un jeton limité et vérifier l’idempotence.
8. Vérifier qu’aucun secret SSH ou API n’apparaît dans les réponses, journaux ou fichiers versionnés.
9. Importer une clé SSH, l’attacher à deux sauvegardes et réussir leurs tests de connexion ; vérifier aussi une clé protégée par phrase secrète et l’exécution planifiée après redémarrage.
10. Refuser une clé invalide, un import contenant uniquement une clé publique et la suppression d’une clé encore utilisée ; changer la clé d’une sauvegarde sans modifier celle des autres.
