
# README

## Environment Variables Reference for Collection and Dataset Promotion

> **Note on the terminology** -
> Our `promote_dataset.py` and `promote_collection.py` scripts are used at two different points in this workflow: Publishing a new dataset/collection to staging and promoting it from staging to production. The script and env var name use the term "promote" generically for both steps. Passing `staging` as the stage argument publishes to the staging catalog, and `production` promotes a collection *from* `staging` to `production`.

| Variable | Used by | Default | Notes |
| -- | -- | -- | -- |
| STAGING_AIRFLOW_API_VERSION | Staging Publish/Promotion | 2 | The Airflow Version for the Staging Environment.  For Airflow 2 = API v1 + Basic auth, Airflow 3 = API v2 + JWT |
| PRODUCTION_AIRFLOW_API_VERSION | Production Promotion | 2 | Same as above |
| AIRFLOW_JWT_SECRET | Any Publish/Promotion step that uses Airflow 3 | none | Conditionally required when the resolved API version is 3. Must match the secret that the Airflow 3 deployment signs with. Can be found in Secrets Manager |
| AIRFLOW_JWT_SUB | Airflow 3 only | none | Conditionally required when the resolved API version is 3. The subject claim for the token (the Integer ID from Airflow's user table) |
