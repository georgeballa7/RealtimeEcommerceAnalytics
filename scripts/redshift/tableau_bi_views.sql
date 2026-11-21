-- RealtimeEcommerceAnalytics - Tableau Redshift curated analytics views
-- Deployed successfully 2026-11-21 (Redshift Data API batch acb697a4-ccd7-49e9-bd82-1850536a157a).
-- Database analytics; schema analytics_bi. No Tableau credentials stored here.
-- First-time installation: CREATE ROLE is not idempotent; omit that one statement on subsequent executions.
-- Refresh upstream dbt marts and analytics_ml.churn_* before querying these late-stage BI views.

-- Step 1: analytics object
CREATE SCHEMA IF NOT EXISTS analytics_bi;

-- Step 2: run once only
CREATE ROLE tableau_analytics_reader;

-- Step 3: analytics object
CREATE OR REPLACE VIEW analytics_bi.v_churn_model_metrics AS
SELECT examples AS holdout_records,positive_examples AS holdout_churn_events,
tp AS true_positives,tn AS true_negatives,fp AS false_positives,fn AS false_negatives,
accuracy,precision,recall,f1,roc_auc,majority_baseline_accuracy
FROM analytics_ml.churn_nb_validation_metrics_v1;

-- Step 4: analytics object
CREATE OR REPLACE VIEW analytics_bi.v_churn_risk_segments AS
SELECT CASE segment WHEN 'Very low (<20%)' THEN 1 WHEN 'Low (20-40%)' THEN 2
WHEN 'Medium (40-60%)' THEN 3 WHEN 'High (60-80%)' THEN 4 ELSE 5 END AS risk_order,
segment AS risk_segment,customer_count,average_predicted_risk
FROM analytics_ml.churn_nb_score_segments_v1;

-- Step 5: analytics object
CREATE OR REPLACE VIEW analytics_bi.v_churn_monthly_labels AS
SELECT CAST(as_of_date AS DATE) AS cohort_month,
COUNT(*) AS records,SUM(churn_next_30d) AS churn_events,
ROUND(100.0*SUM(churn_next_30d)::DECIMAL(18,5)/NULLIF(COUNT(*),0),2) AS observed_churn_pct
FROM analytics_ml.churn_features_v1 GROUP BY 1;

-- Step 6: analytics object
CREATE OR REPLACE VIEW analytics_bi.v_churn_model_calibration AS
WITH ranked AS (
 SELECT churn_probability,actual_label,NTILE(10) OVER (ORDER BY churn_probability) AS decile
 FROM analytics_ml.churn_nb_predictions_v1 WHERE cohort='validation')
SELECT decile,COUNT(*) AS records,
ROUND(100.0*AVG(churn_probability),2) AS predicted_churn_pct,
ROUND(100.0*SUM(actual_label)::DECIMAL(18,5)/NULLIF(COUNT(*),0),2) AS observed_churn_pct
FROM ranked GROUP BY 1;

-- Step 7: analytics object
CREATE OR REPLACE VIEW analytics_bi.v_churn_risk_by_recency AS
SELECT CASE WHEN s.recency_days<=30 THEN '0-30 days'
WHEN s.recency_days<=60 THEN '31-60 days'
WHEN s.recency_days<=90 THEN '61-90 days' ELSE '91+ days' END AS recency_segment,
COUNT(*) AS customers,ROUND(100.0*AVG(p.churn_probability),2) AS mean_predicted_churn_pct
FROM analytics_ml.churn_nb_predictions_v1 p
JOIN analytics_marts.mart_customer_churn_scoring s
ON s.customer_key=p.customer_key AND s.as_of_date=p.as_of_date
WHERE p.cohort='current' GROUP BY 1;

-- Step 8: analytics object
CREATE OR REPLACE VIEW analytics_bi.v_monthly_sales AS
SELECT CAST(DATE_TRUNC('month',order_timestamp_utc) AS DATE) AS sales_month,
source_system,COUNT(*) AS orders,ROUND(SUM(current_gbp),2) AS net_sales_gbp
FROM analytics_marts.fct_orders GROUP BY 1,2;

-- Step 9: analytics object
CREATE OR REPLACE VIEW analytics_bi.v_customer_churn_scores AS
SELECT p.customer_key,p.as_of_date,p.churn_probability,
p.predicted_label,
CASE WHEN p.churn_probability>=0.8 THEN 'Very high'
WHEN p.churn_probability>=0.6 THEN 'High'
WHEN p.churn_probability>=0.4 THEN 'Medium'
WHEN p.churn_probability>=0.2 THEN 'Low' ELSE 'Very low' END AS risk_segment,
s.recency_days,s.order_count_90d,s.spend_90d_gbp,s.lifetime_order_count
FROM analytics_ml.churn_nb_predictions_v1 p
JOIN analytics_marts.mart_customer_churn_scoring s
ON s.customer_key=p.customer_key AND s.as_of_date=p.as_of_date
WHERE p.cohort='current';

-- Step 10: role authorization
GRANT USAGE ON SCHEMA analytics_bi TO ROLE tableau_analytics_reader;

-- Step 11: role authorization
GRANT SELECT ON ALL TABLES IN SCHEMA analytics_bi TO ROLE tableau_analytics_reader;

-- Additional persistent scoped SELECT permission for BI views created later:
GRANT SELECT FOR TABLES IN SCHEMA analytics_bi TO ROLE tableau_analytics_reader;
