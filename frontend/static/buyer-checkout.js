(() => {
    const form = document.getElementById('buyForm');
    if (!form) return;
    const button = document.getElementById('placeOrderButton');
    const status = document.getElementById('quoteStatus');
    const quoteButton = document.getElementById('refreshQuote');
    const fingerprint = () => ['quantity', 'delivery_latitude', 'delivery_longitude'].map(name => form.elements[name].value).join('|');
    let quotedInput = null, requestId = 0, submitting = false;
    const invalidate = () => {
        button.disabled = true;
        quotedInput = null;
        form.elements.quote_token.value = '';
        document.getElementById('estimatedTotal').textContent = '—';
        document.getElementById('logisticsCost').textContent = 'Calculate the estimate for your selected location and quantity';
    };
    form.addEventListener('input', event => {
        if (['quantity', 'delivery_latitude', 'delivery_longitude'].includes(event.target.name)) invalidate();
    });
    quoteButton.addEventListener('click', async () => {
        invalidate();
        const current = fingerprint(), id = ++requestId;
        quoteButton.disabled = true;
        status.textContent = 'Calculating delivery estimate…';
        try {
            const response = await fetch(form.dataset.quoteUrl, {method: 'POST', body: new FormData(form), headers: {'Accept': 'application/json'}});
            if (response.redirected) throw new Error('Your session expired. Sign in again before ordering.');
            if (!(response.headers.get('content-type') || '').includes('application/json')) {
                throw new Error('Unable to calculate the estimate. Refresh the page and try again.');
            }
            const data = await response.json();
            if (id !== requestId || current !== fingerprint()) {
                status.textContent = 'Quantity or location changed. Calculate the estimate again.';
                return;
            }
            if (!response.ok) throw new Error(data.error || 'Unable to calculate the estimate.');
            document.getElementById('productTotal').textContent = data.product_total;
            document.getElementById('logisticsCost').textContent = `₹${data.estimated_logistics_cost} (${data.distance_km} km, straight-line estimate)`;
            document.getElementById('estimatedTotal').textContent = data.total_amount;
            status.textContent = 'Review your delivery details and estimated total before placing the order.';
            quotedInput = current;
            form.elements.quote_token.value = data.quote_token;
            button.disabled = false;
        } catch (error) {
            status.textContent = error.message || 'Unable to calculate the estimate. Please try again.';
        } finally {
            quoteButton.disabled = false;
        }
    });
    form.addEventListener('submit', event => {
        if (event.defaultPrevented) return;
        if (submitting || quotedInput !== fingerprint()) {
            event.preventDefault();
            if (!submitting) { invalidate(); status.textContent = 'Recalculate the estimate after changing quantity or location.'; }
            return;
        }
        submitting = true;
        button.disabled = true;
        button.textContent = 'Placing order…';
    });
    window.addEventListener('pageshow', () => {
        submitting = false;
        button.textContent = 'Place Order';
        invalidate();
    });
})();
