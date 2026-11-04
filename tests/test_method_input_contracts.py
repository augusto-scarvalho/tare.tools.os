"""New user-file contracts for existing offline delivery study readers."""
import copy,json
import pytest
from relay import delivery_costs,delivery_comparison

def file(tmp_path,value):
    path=tmp_path/'input.json';path.write_text(json.dumps(value),encoding='utf-8');return path

def study():
    case={'instruction':'A current product requirement','baseline':'current-source','checks':{'check':['python','-V']}}
    return {'schema':delivery_comparison.SCHEMA,'cohort':'input-contracts','baseline':'A',
        'cases':{'one':copy.deepcopy(case),'two':copy.deepcopy(case)},
        'candidates':[{'id':'A'},{'id':'B'}],
        'trials':[{'candidate':a,'case':c,'sessions':[]} for a in ('A','B') for c in ('one','two')]}

class TestCostDates:
    @pytest.mark.parametrize('value',[True,1,[],{},['2026-12-01']])
    def test_bad_expiry_type_is_valueerror(self,value):
        from compute_plane.choice_assessment import digest
        profile={'id':'fixture'};entry={'usage':None,'attempts':[]}
        quote={'profile_sha256':digest(profile),'currency':'USD','source':'frozen study','observed_at':'2026-10-22','mode':'unknown','valid_until':value}
        with pytest.raises(ValueError):delivery_costs.apply(entry,profile,{'profiles':{'fixture':quote}})

class TestCostProfiles:
    @pytest.mark.parametrize('profiles',[None,[],1,'x',True])
    def test_reject_nonmapping_profiles(self,tmp_path,profiles):
        value={'schema':'tare.tools/delivery-costs/1','basis':'estimated_delivery_cost','profiles':profiles}
        with pytest.raises(ValueError):delivery_costs.load(str(file(tmp_path,value)))
    def test_require_profiles(self,tmp_path):
        with pytest.raises(ValueError):delivery_costs.load(str(file(tmp_path,{'schema':'tare.tools/delivery-costs/1','basis':'estimated_delivery_cost'})))

class TestCostQuote:
    @pytest.mark.parametrize('quote',[[],None,1,'text',True])
    def test_bad_quote_is_valueerror_without_mutation(self,quote):
        entry={'usage':None,'attempts':[]};before=copy.deepcopy(entry)
        with pytest.raises(ValueError):delivery_costs.apply(entry,{'id':'fixture'},{'profiles':{'fixture':quote}})
        assert entry==before
    def test_none_snapshot_keeps_existing_identity(self):
        entry={'usage':None};assert delivery_costs.apply(entry,{'id':'fixture'},None) is entry

class TestCostCalls:
    @pytest.mark.parametrize('call',[None,[],1,'text',True])
    def test_bad_call_record_is_valueerror(self,call):
        from compute_plane.choice_assessment import digest
        profile={'id':'fixture'};entry={'usage':{'calls':[call]},'attempts':[]};before=copy.deepcopy(entry)
        quote={'profile_sha256':digest(profile),'currency':'USD','source':'frozen study','observed_at':'2026-10-22','mode':'unknown'}
        with pytest.raises(ValueError):delivery_costs.apply(entry,profile,{'profiles':{'fixture':quote}})
        assert entry==before

class TestHourlyAttempts:
    @pytest.mark.parametrize('value',[None,[],1,'text',True])
    def test_bad_invocation_record_is_valueerror(self,value):
        from compute_plane.choice_assessment import digest
        profile={'id':'fixture'};entry={'usage':{'calls':[{'execution':'started'}]},'attempts':[value]};before=copy.deepcopy(entry)
        quote={'profile_sha256':digest(profile),'currency':'USD','source':'frozen study','observed_at':'2026-10-22','mode':'hourly','usd_per_hour':'1'}
        with pytest.raises(ValueError):delivery_costs.apply(entry,profile,{'profiles':{'fixture':quote}})
        assert entry==before

class TestStudyCases:
    @pytest.mark.parametrize('value',[None,[],1,'text',True])
    def test_case_record_is_valueerror_before_profiles(self,tmp_path,monkeypatch,value):
        data=study();data['cases']['one']=value
        monkeypatch.setattr(delivery_comparison,'profiles',lambda *a:pytest.fail('Malformed study reached profile resolution'))
        with pytest.raises(ValueError):delivery_comparison.compare(file(tmp_path,data))

class TestStudyCandidates:
    @pytest.mark.parametrize('value',[None,[],1,'text',True,{}, {'id':[]},{'id':''},{'id':' '},{'id':False}])
    def test_bad_candidate_is_valueerror_before_profiles(self,tmp_path,monkeypatch,value):
        data=study();data['candidates'][0]=value
        monkeypatch.setattr(delivery_comparison,'profiles',lambda *a:pytest.fail('Malformed study reached profile resolution'))
        with pytest.raises(ValueError):delivery_comparison.compare(file(tmp_path,data))

class TestStudyTrials:
    @pytest.mark.parametrize('value',[None,{},1,'text',True,[None],[[]],[{}],[{'candidate':[],'case':'one'}]])
    def test_bad_trials_are_valueerror_before_profiles(self,tmp_path,monkeypatch,value):
        data=study();data['trials']=value
        monkeypatch.setattr(delivery_comparison,'profiles',lambda *a:pytest.fail('Malformed study reached profile resolution'))
        with pytest.raises(ValueError):delivery_comparison.compare(file(tmp_path,data))
