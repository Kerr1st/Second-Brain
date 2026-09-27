"""Pure rollout assembly does not need SQLite, files, tools or models."""
from src.capture.sources.codex_turns import RolloutTurnAssembler


def test_aborted_legacy_prompt_cannot_pair_with_a_later_answer():
    assembler=RolloutTurnAssembler('synthetic',legacy=True)
    assembler.feed({'type':'event_msg','payload':{'type':'user_message','message':'cancelled','client_id':'u1'}})
    assembler.feed({'type':'event_msg','payload':{'type':'turn_aborted','turn_id':'t1'}})
    assembler.feed({'type':'response_item','payload':{'type':'message','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':'later answer'}]}})
    assert assembler.turns == []
    assert assembler.drops['aborted_legacy_prompts'] == 1


def test_legacy_is_explicitly_enabled_and_unknown_events_are_counted():
    assembler=RolloutTurnAssembler('synthetic')
    assembler.feed({'type':'event_msg','payload':{'type':'user_message','message':'old','client_id':'u1'}})
    assembler.feed({'type':'response_item','payload':{'type':'reasoning','content':'hidden'}})
    assert assembler.drops['legacy_disabled'] == 1
    assert assembler.drops['non_conversation_record'] == 1
    assert assembler.turns == []
