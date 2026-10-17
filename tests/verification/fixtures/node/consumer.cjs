exports.display = response => '$' + (response.amount / 100).toFixed(2);
exports.unrelatedDiscount = amount => amount / 100;
