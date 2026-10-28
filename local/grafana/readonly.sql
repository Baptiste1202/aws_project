-- Compte de lecture de Grafana (stack locale seulement) : lecture de toutes les tables
-- de toutes les bases (shop pour la VM, shop_hybrid pour l'hybride), aucune écriture.
-- Exécuté une fois, à la création du volume PostgreSQL (docker-entrypoint-initdb.d).
CREATE ROLE grafana_reader LOGIN PASSWORD 'grafana-local-pass';
GRANT pg_read_all_data TO grafana_reader;
