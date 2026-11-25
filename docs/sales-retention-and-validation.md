# Sales, customer retention and validation

A reporting month can show more orders and less sales value at the same time. The order count measures purchases recorded during the month. The sales value sums the current value of those orders, which can change after purchase. The development store and historical simulation provide two distinct sources for exploring this behavior.

## What changed in sales?

The Tableau monthly sales view groups orders by the month of the original order timestamp and by source. Its `net_sales_gbp` column sums `current_gbp`. An adjustment to a March order can therefore change the March figure in a later refresh. The view cannot establish when cash moved.

| Question | Dataset and grain | Interpretation |
| --- | --- | --- |
| How many orders were placed, and what are they worth now? | `analytics_bi.v_monthly_sales`, one row per month and source | Compare order count and current GBP value for the same source and month. |
| Which items drove volume? | `analytics_marts.fct_order_lines`, one row per order line | Sum quantities at line grain. Joining an order total onto three lines repeats that total three times. |
| What was cancelled or refunded? | `fct_cancellations`, one simulated cancellation event. `fct_refund_lines`, one recorded Shopify refund line. | Report simulated cancellations and recorded refund subtotals separately. Subtracting them again from a current order value can double-count an adjustment. |

```sql
SELECT sales_month, source_system, orders, net_sales_gbp
FROM analytics_bi.v_monthly_sales
ORDER BY sales_month DESC, source_system;
```

## Verify a reporting refresh

Choose a Europe/Berlin processing date and slot, then carry its batch ID through the checks. A successful webhook response establishes queue acceptance. A reporting refresh requires all of the following evidence.

| Boundary | Evidence for the same slot | What it establishes |
| --- | --- | --- |
| PostgreSQL → Glue | Required generation and lifecycle markers, settled projection, available RDS instance and active extraction lease | Source writes have settled before the five JDBC reads begin. Reads across tables lack a shared PostgreSQL transaction snapshot. |
| Bronze → Silver | Source, accepted and quarantined counts by table. Unique business keys, valid parent references, verified identities and exact GBP amounts. | Each input row is accounted for. Inspect quarantine reasons before interpreting missing sales. |
| Silver → Redshift | Published manifest key and digest, matching warehouse load ledger and reconciled staging counts | All five live Shopify tables were replaced together in the warehouse transaction. Historical backfill has a separate load path. |
| Redshift → Tableau | Warehouse acknowledgement, matching dbt task with exit code zero, passing data tests, and workbook date/source filters and refresh mode | The expected batch reached the marts and the workbook is displaying the intended slice. |

If orders are missing, locate the first boundary without matching evidence. Replaying a Shopify transaction can create a second external write. Start with the existing delivery and batch identifiers. [Slot verification and recovery](operations/README.md#verify-a-slot-with-read-only-access) · [Silver table contract](data-model/README.md#live-shopify-silver-contract)

## Which customers are likely to go 30 days without another order?

At each eligible monthly cutoff, a customer with a prior order receives a label of 1 if they place zero orders in the following 30 days. Training includes only cutoffs whose full outcome window is observable. A customer can buy again later, so “churn” here has a precise, time-limited meaning.

The model uses four facts available from the order history: days since the last order, order count and spend over the preceding 90 days, and lifetime order count. Redshift SQL groups each feature into bands and fits a categorical Naive Bayes classifier with Laplace smoothing. The current scoring cutoff is the day after the latest observed order. dbt refreshes the features, and the SQL prediction view applies the stored model parameters. Refitting those parameters is a separate operation.

## How much confidence does a score deserve?

The November 21 evaluation used 15,053 customer-month examples for training and 7,524 later examples for evaluation. The model identified **91.66%** of customers who went inactive in the holdout. Among the 6,523 examples it flagged, 1,382 customers actually placed another order. Accuracy exceeded the majority-class baseline by **0.86 percentage points**.

The scores support exploration of relative risk in this synthetic population. In the highest-scoring tenth, the mean prediction was about 96.9% and the observed inactivity rate was 81.8%. Treat a score as a ranking signal when comparing segments.

### Reproduce the evaluation

The SQL fit estimates class priors and feature likelihoods from customer-months before September 2026. The recorded evaluation uses September and October 2026, preserving time order. The 30-day label looks forward from each cutoff. The feature window looks backward. Historical spend uses current order values, so later adjustments can affect reconstruction of spend as it was known at the cutoff.

| November 21, 2026 holdout | Result |
| --- | ---: |
| Customer-months | 7,524 |
| True positives / false positives | 5,141 / 1,382 |
| True negatives / false negatives | 533 / 468 |
| Precision / recall / F1 | 0.7881 / 0.9166 / 0.8475 |
| Accuracy / majority-class accuracy | 0.7541 / 0.7455 |
| ROC-AUC | 0.6398 |

Use a read-only session in the `analytics` database to reproduce the evaluation for the currently stored parameters. The first query measures holdout performance. The second compares predicted risk to observed outcomes. The third describes the scoring population at the latest available feature cutoff.

```sql
SELECT * FROM analytics_bi.v_churn_model_metrics;
SELECT * FROM analytics_bi.v_churn_model_calibration;
SELECT * FROM analytics_ml.churn_nb_score_segments_v1;
```

The [training SQL](../scripts/redshift/train_churn_naive_bayes.sql) rebuilds `analytics_ml.churn_nb_priors_v1` and `analytics_ml.churn_nb_likelihoods_v1` and recreates dependent views. Run it only for an intended refit after validating the source marts. Training uses Redshift query compute. Record the evaluation date and population when comparing model versions.

## Explore in Tableau

Start with `analytics_bi.v_monthly_sales` for sales, `v_churn_risk_segments` for the current distribution, and `v_churn_model_calibration` to compare predicted and observed risk on the holdout. The November 21 baseline recorded 4,417 scored customers. The current population changes as new orders reach the marts. For a dashboard comparison, match the reporting month and source and verify the refresh for the processing slot.

[Connect Tableau](operations/README.md#connect-tableau-to-private-redshift) · [Data grains and ERDs](data-model/README.md) · [Serving view definitions](../scripts/redshift/tableau_bi_views.sql)

## Recorded engineering checks

| Checkpoint | Evidence and scope |
| --- | --- |
| November 21, 2026 | Glue, Redshift loading and dbt completed for the recorded run. Classifier evaluation above is a dated baseline. |
| November 23–24, 2026 | Infrastructure review established network and ownership evidence and recorded legacy resource retirement. |
| November 24 documentation review | Against source baseline `09f9b684`, the review inventoried 319 branch files, checked internal links, rendered five AWS figures and seven Mermaid diagrams, and checked ERDs against migrations and dbt. Ruff and script compilation passed. The unit suite recorded 420 passes and one skip. Live queries and deployment commands were outside that review. |

For a change to a Silver rule, run the [Glue contract tests](../tests/unit/test_phase6_glue_silver_contract.py) and inspect quarantine counts for the affected table. For a metric change, inspect its [dbt model and tests](../analytics/dbt) and repeat the reporting comparison at the documented grain.
