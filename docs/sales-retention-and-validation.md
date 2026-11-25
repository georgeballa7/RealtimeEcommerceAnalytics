# Sales, customer retention and validation

A reporting month can show more orders and less sales value at the same time. The order count measures purchases recorded during the month. The sales value sums the current value of those orders, which can change after purchase. The development store and historical simulation provide two distinct sources for exploring this behavior.

## What changed in sales?

The Tableau monthly sales view groups orders by the month of the original order timestamp and by source. Its `net_sales_gbp` column sums `current_gbp`. An adjustment to a March order can therefore change the March figure in a later refresh. The view cannot establish when cash moved.

| Question | Dataset and grain | Interpretation |
| --- | --- | --- |
| How many orders were placed, and what are they worth now? | `analytics_ml.tableau_lb_v_monthly_sales`, one row per month and source | Compare order count and current GBP value for the same source and month. |
| Which items drove volume? | `analytics_marts.fct_order_lines`, one row per order line | Sum quantities at line grain. Joining an order total onto three lines repeats that total three times. |
| What was cancelled or refunded? | `fct_cancellations`, one simulated cancellation event. `fct_refund_lines`, one recorded Shopify refund line. | Report simulated cancellations and recorded refund subtotals separately. Subtracting them again from a current order value can double-count an adjustment. |

```sql
SELECT sales_month, source_system, orders, net_sales_gbp
FROM analytics_ml.tableau_lb_v_monthly_sales
ORDER BY sales_month DESC, source_system;
```

## Verify a reporting refresh

Compare the same reporting month and source. For a live connection, trace the slot to its warehouse acknowledgement and successful dbt build. For an extract or CSV, also check when the workbook data was refreshed. The [slot verification guide](operations/README.md#verify-a-slot-with-read-only-access) gives the checks in order.

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
SELECT examples AS holdout_records, positive_examples AS holdout_churn_events,
       tp AS true_positives, tn AS true_negatives, fp AS false_positives,
       fn AS false_negatives, accuracy, precision, recall, f1, roc_auc,
       majority_baseline_accuracy
FROM analytics_ml.churn_nb_validation_metrics_v1;

WITH ranked AS (
    SELECT NTILE(10) OVER (ORDER BY churn_probability, customer_key, as_of_date) AS decile,
           churn_probability, actual_label
    FROM analytics_ml.churn_nb_predictions_v1
    WHERE cohort = 'validation'
)
SELECT decile, COUNT(*) AS records,
       ROUND(100.0 * AVG(churn_probability), 2) AS predicted_churn_pct,
       ROUND(100.0 * AVG(actual_label::float), 2) AS observed_churn_pct
FROM ranked
GROUP BY decile
ORDER BY decile;

SELECT segment, customer_count, average_predicted_risk
FROM analytics_ml.churn_nb_score_segments_v1
ORDER BY average_predicted_risk DESC;
```

The [training SQL](../scripts/redshift/train_churn_naive_bayes.sql) rebuilds `analytics_ml.churn_nb_priors_v1` and `analytics_ml.churn_nb_likelihoods_v1` and recreates dependent views. Run it only for an intended refit after validating the source marts. Training uses Redshift query compute. Record the evaluation date and population when comparing model versions.

## Explore in Tableau

The workbook reads monthly sales from `analytics_ml.tableau_lb_v_monthly_sales`. For the risk distribution it counts distinct customers by risk band at the latest scoring date in `analytics_ml.tableau_lb_v_customer_churn_scores`. For calibration it calculates deciles directly from validation predictions, using customer and cutoff date to break ties in probability. The November 21 baseline recorded 4,417 scored customers. The current population changes as new orders reach the marts. For a dashboard comparison, match the reporting month and source and verify the refresh for the processing slot.

[Connect Tableau](operations/README.md#connect-tableau-to-private-redshift) · [Data grains and ERDs](data-model/README.md) · [Serving view definitions](../scripts/redshift/tableau_bi_views.sql)

## Recorded engineering checks

| Checkpoint | Evidence and scope |
| --- | --- |
| November 21, 2026 | Glue, Redshift loading and dbt completed for the recorded run. Classifier evaluation above is a dated baseline. |
| November 23–24, 2026 | Infrastructure review established network and ownership evidence and recorded legacy resource retirement. |
| November 24 documentation review | Against source baseline `09f9b684`, the review inventoried 319 branch files, checked internal links, rendered five AWS figures and seven Mermaid diagrams, and checked ERDs against migrations and dbt. Ruff and script compilation passed. The unit suite recorded 420 passes and one skip. Live queries and deployment commands were outside that review. |

For a change to a Silver rule, run the [Glue contract tests](../tests/unit/test_phase6_glue_silver_contract.py) and inspect quarantine counts for the affected table. For a metric change, inspect its [dbt model and tests](../analytics/dbt) and repeat the reporting comparison at the documented grain.
