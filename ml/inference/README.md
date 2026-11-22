# Churn scoring

The implemented baseline scores customer churn risk in Redshift using SQL Naive Bayes (`analytics_ml`) over dbt feature marts. Its validation metrics and Tableau-facing views are separate from any future Python/MLflow experimentation.

The baseline uses synthetic data and must not be presented as calibrated real-customer risk.
