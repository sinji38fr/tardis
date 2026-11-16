# Avancement

## 0.1 — Configuration

- [x] Interface française avec accès direct sans connexion.
- [x] URL publique, sous-chemin, adresse d’écoute et port configurables.
- [x] Serveurs manuels, édition et désactivation.
- [x] Import de clés privées SSH par fichier ou collage, avec phrase secrète facultative.
- [x] Chiffrement des clés au repos ; métadonnées publiques uniquement dans les réponses.
- [x] Sauvegarde = serveur + login + clé + dossiers, exclusions et politique enregistrés.
- [x] Révisions indépendantes et activité persistante en SQLite.
- [x] Déploiement Docker Compose.

## Prochaine étape — Collecte réelle

- [x] Worker et coffre isolé ; migration du chiffrement sans perte des clés déjà importées.
- [x] Empreinte SSH à approuver et test de lisibilité des dossiers.
- [x] Génération de configuration rsnapshot et collecte manuelle avec journaux.
- [x] File persistante, verrous et reprise après incident.
- [x] Ordonnanceur, rétention et publication des versions réussies uniquement.
- [x] Contrôle d’espace disque : refus à 95 %.
- [x] Affichage de la consommation physique adaptée aux hard links.
- [x] Navigation et téléchargement de fichiers depuis les versions.
- [x] Restauration vers un serveur source avec aperçu.
- [x] Tests d’intégration SSH/rsync/rsnapshot dans un environnement isolé.
- [x] Webhooks et contrôles de contenu.

## 0.2 — Protection SQLite et moteurs

- [x] Copie SQLite cohérente à chaud au démarrage et périodiquement, indépendante des transferts.
- [x] Contrôle d’intégrité, SHA256, rétention, téléchargement et copie avant migration.
- [x] Adaptateurs de moteurs et sélection par sauvegarde ; rsnapshot disponible.
- [x] Options JSON et registre extensible sans migration par moteur.
- [x] Configuration historique 0.1 conservée et migration automatique.
- [x] Purge des caches de travail des anciennes révisions.

## 1.0 — Distribution installable

- [x] Coffre d’import public et clés de déchiffrement isolées du Web.
- [x] Restauration de fichiers/dossiers avec aperçu, confirmation et protection contre les collisions.
- [x] Référence et contrôles SHA256 manuels.
- [x] Notifications signées, persistantes et réessayées.
- [x] Comptage des blocs par inode et purge de caches protégés par les tâches actives.
- [x] Dépendances verrouillées et manifestes Compose pour une image publiée.
- [x] Tests d’intégration et CI GitHub.
