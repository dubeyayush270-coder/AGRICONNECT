# Delivery frontend contract

You can redesign HTML/CSS independently. Preserve existing Flask form actions,
input `name` values, JavaScript element IDs, and Jinja variables. This change adds
backend APIs; the new status buttons still need to be connected in the frontend.

## Driver list and controls

- `GET /logistics/deliveries?view=active&page=1` (default)
- `GET /logistics/deliveries?view=history&page=1`
- Requires a logged-in Logistics user. Returns only that driver's assignments.
- Response: `success`, `deliveries`, `summary`, `csrf_token`, `page`, `has_next`, `view`.
- Each delivery has `order_id`, `crop_name`, `quantity`, `status`, `next_status`,
  pickup/delivery addresses and coordinates, supplier/buyer names and phones,
  product amount, distance and estimated delivery/total amounts.
- `logistics_assigned_at`, `picked_up_at`, `in_transit_at`, `delivered_at` are
  Unix seconds or null. Convert with `new Date(value * 1000)`.
- Decimal amounts/coordinates may be JSON strings; use `Number(value)` for display calculations.
- Summary: `active_count`, `completed_count`, `completed_delivery_estimate`.
  The last value is the sum of estimated charges on delivered jobs, not a payment balance.

Show one next-action button per active order:

| Current status | Button | Submit status |
| --- | --- | --- |
| LOGISTICS_ASSIGNED | Mark picked up | PICKED_UP |
| PICKED_UP | Start delivery | IN_TRANSIT |
| IN_TRANSIT | Mark delivered | DELIVERED |
| DELIVERED / CANCELLED | None | None |

```javascript
const listResponse = await fetch('/logistics/deliveries');
const list = await listResponse.json();
// Call this on the assigned order's next-action button click.
const response = await fetch(`/logistics/deliveries/${order.order_id}/status`, {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-CSRF-Token': list.csrf_token},
  body: JSON.stringify({status: order.next_status})
});
const result = await response.json();
// Show result.message, then fetch the list again on success or HTTP 409.
```

Disable the button while saving. Success returns `changed`, `order_id`, `status`,
`next_status`, `message`. Repeating the current status is safe and preserves its
timestamp. Skips/backward steps return 409. Nonassigned/missing orders return 404;
login/role/CSRF errors return 401/403. Database failures return 503.

The existing `/logistics` Jinja context retains readable `active_deliveries` and
`delivery_history` strings. For redesigned cards, loop through the structured
`active_delivery_records` / `delivery_history_records` instead. The token is
`delivery_csrf_token`. These dashboard lists show the first 20 rows; use the API
for further pages. Render user-entered text with Jinja escaping or `textContent`.

## Buyer views

Existing `/buyer/orders`, `/buyer/orders/<id>` and
`/buyer/orders/<id>/location` reflect the saved status. Location sharing ends
when the order is delivered or cancelled. Buyer dashboard pending counts and
delivered order value update on the next request; no payment is recorded by
marking a job delivered.

## Database setup

Run `python -B migrations/add_delivery_timestamps.py --apply` from `frontend`
once on each database before serving the new routes. It adds three nullable
timestamps without altering existing orders and is safe to run again.
