from django import forms

from . import models


class LeadTimeOverrideForm(forms.Form):
    sku = forms.CharField(label='Component', max_length=60)
    lead_time_days = forms.IntegerField(label='Lead time (days)', min_value=1, max_value=730)
    note = forms.CharField(label='Reason', max_length=255, required=False)

    def clean_sku(self):
        sku = self.cleaned_data['sku'].strip()
        if not models.StockItem.objects.filter(sku=sku, kind=models.StockItem.COMPONENT).exists():
            raise forms.ValidationError('Unknown component SKU.')
        return sku
