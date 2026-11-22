# SQL-only churn model (v1)

**Deployed:** 2026-11-21 into Redshift Serverless database `analytics`, schema `analytics_ml`.

This is a real **supervised categorical Naive Bayes classifier implemented with SQL**—not Amazon Redshift ML `CREATE MODEL`. The four pre-outcome predictors are order recency, frequency over 90 days, spend over 90 days, and lifetime order count. Bin counts and Laplace-smoothed class likelihoods are persisted in `churn_nb_priors_v1` and `churn_nb_likelihoods_v1`; the `churn_nb_predictions_v1`, `churn_nb_validation_metrics_v1`, and `churn_nb_score_segments_v1` views expose scoring and evaluation.

**Training:** 15,053 customer-month records before September 2026. **Temporal validation:** 7,524 records in September–October 2026. **Scoring:** Customer population is recalculated from the latest completed dbt mart; it varies with incoming events. The last verified evening baseline had 4,417 scored customers.

Results (temporal holdout): ROC-AUC **0.6398**, accuracy **0.7541**, precision **0.7881**, recall **0.9166**, F1 **0.8475**; majority-class accuracy **0.7455**. Confusion matrix: TP 5141, TN 533, FP 1382, FN 468. This is only a modest uplift over baseline, and **model scores are not calibrated** (in top decile approx. 96.9% score vs 81.8% observed). Treat model as a learning baseline on synthetic data, not as a production decision engine.

Run `scripts/redshift/train_churn_naive_bayes.sql` in Redshift Query Editor v2 or through the Redshift Data API with authenticated warehouse access. Recompute model parameter tables after refreshing training data; calculate holdout quality before relying on scored outputs. Cost note: SQL training incurs Redshift query compute only. Redshift ML `CREATE MODEL` would invoke separately billed SageMaker training, so do not start it without explicitly evaluating budget headroom.

Example SQL:

```sql
SELECT * FROM analytics_ml.churn_nb_validation_metrics_v1;
SELECT * FROM analytics_ml.churn_nb_score_segments_v1;
SELECT customer_key, churn_probability
FROM analytics_ml.churn_nb_predictions_v1
WHERE cohort = 'current' ORDER BY churn_probability DESC LIMIT 20;
```

## Tableau

Tableau Desktop connects to the private Redshift workgroup using the SSM access tunnel and read-only role; it can query `analytics_bi`, `analytics_marts`, `analytics_ml` and other granted application schemas. Do not publish customer identifiers to Tableau Public. See `docs/tableau_redshift_connectivity.md`.
