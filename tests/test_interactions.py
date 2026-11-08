"""Synthetic widget fixtures: no real challenge, account, browser or network."""
import io
from unittest.mock import Mock

import pytest
from PIL import Image, ImageDraw

from captcha.extractor import find_grid, tile_centers_page
from captcha.interactions import DynamicInteractions, selection_marks
from inss import session

GRID=(10,130,380,380)


def widget(selected=()):
    image=Image.new('RGB',(400,600),'white')
    draw=ImageDraw.Draw(image)
    draw.rectangle((10,10,390,110),fill=(0,131,143))
    draw.text((20,24),'Select the matching images',fill='white')
    photo=Image.new('RGB',(120,120))
    photo.putdata([(80+x,70+y,90+(x+y)//2) for y in range(120) for x in range(120)])
    for index in range(9):
        x,y=10+130*(index%3),130+130*(index//3)
        image.paste(photo,(x,y))
        if index+1 in selected:
            draw.rectangle((x,y,x+119,y+119),fill='white')
            image.paste(photo.resize((100,100)),(x+10,y+10))
            cx,cy=x+97,y+23
            draw.ellipse((cx-8,cy-8,cx+8,cy+8),fill=(0,131,143))
            draw.line([(cx-4,cy),(cx-1,cy+3),(cx+4,cy-3)],fill='white',width=2)
    return image


@pytest.mark.parametrize('selected',[set(),{2},{1,5,9}])
def test_detects_exact_selected_tiles(selected):
    before=widget()
    assert selection_marks(before,widget(selected),GRID)==selected
    assert selection_marks(before,before.resize((200,300)),GRID)==set()


def test_image_change_without_check_marker_is_not_selection():
    before=widget()
    hover=before.copy()
    ImageDraw.Draw(hover).rectangle((20,140,115,235),fill='pink')
    assert selection_marks(before,hover,GRID)==set()


def test_teal_photos_do_not_move_grid_or_hide_selection_marks():
    before = widget()
    # The broad teal band belongs to two photos well below the real header.
    draw = ImageDraw.Draw(before)
    draw.rectangle((16, 190, 128, 239), fill=(0, 130, 155))
    draw.rectangle((146, 190, 258, 239), fill=(0, 130, 155))
    clean_grid = find_grid(widget())
    grid = find_grid(before)
    assert grid == clean_grid
    clip = {'x': 0, 'y': 0, 'width': 400, 'height': 600}
    centers = tile_centers_page({}, clip, grid)
    assert 130 < centers[0][1] < 250
    assert 390 < centers[-1][1] < 510
    after = widget({1, 6, 7})
    # Preserve the teal photo in the unselected second tile.
    after.paste(before.crop((146, 190, 262, 240)), (146, 190))
    assert selection_marks(before, after, grid) == {1, 6, 7}


def test_header_band_tolerates_a_short_text_gap():
    image = widget()
    ImageDraw.Draw(image).rectangle((10, 45, 390, 46), fill='white')
    assert find_grid(image) == find_grid(widget())


def test_loading_tiles_are_not_ready_for_vision():
    controller=DynamicInteractions()
    before=widget()
    assert controller.images_ready(before,GRID)
    loading=before.copy()
    ImageDraw.Draw(loading).rectangle((10,130,390,510),fill=(240,240,240))
    assert not controller.images_ready(loading,GRID)


def test_wait_exits_when_exact_selection_has_stabilized(monkeypatch):
    from captcha import interactions
    clock=[0.0]
    monkeypatch.setattr(interactions.time,'monotonic',lambda:clock[0])
    data=io.BytesIO()
    widget({2,5}).save(data,format='PNG')
    page=Mock()
    page.screenshot.return_value=data.getvalue()
    page.wait_for_timeout.side_effect=lambda ms:clock.__setitem__(0,clock[0]+ms/1000)
    controller=DynamicInteractions(timeout_ms=500)
    controller.prepare(widget(),GRID,[2,5])
    assert controller.wait_ready(page,{'x':0,'y':0,'width':400,'height':600})
    assert 180<=controller.metrics[-1]['pre_submit_wait_ms']<500
    controller.prepare(widget(),GRID,[2])
    assert not controller.wait_ready(page,{})  # extra selected tile cannot be ignored


def test_short_click_retains_movement_and_small_fixed_margins():
    page=Mock()
    DynamicInteractions(short_clicks=True).click(page,100,200)
    assert page.mouse.move.call_args.kwargs['steps']==12
    page.mouse.click.assert_called_once_with(100,200)
    waits=[call.args[0] for call in page.wait_for_timeout.call_args_list]
    assert 80<=waits[0]<=120 and 100<=waits[1]<=160


def test_two_matching_frames_near_deadline_confirm_stable_selection(monkeypatch):
    from captcha import interactions
    clock = [0.0]
    monkeypatch.setattr(interactions.time, 'monotonic', lambda: clock[0])
    missing, complete = io.BytesIO(), io.BytesIO()
    widget({2}).save(missing, format='PNG')
    widget({2, 5}).save(complete, format='PNG')
    count = [0]
    # Simulate a slowly rendering worker: first four frames still miss the last click.
    def screenshot(**kwargs):
        clock[0] += .4
        count[0] += 1
        return missing.getvalue() if count[0] <= 4 else complete.getvalue()
    page = Mock()
    page.screenshot.side_effect = screenshot
    page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
    controller = DynamicInteractions(timeout_ms=2500)
    controller.prepare(widget(), GRID, [2, 5])
    assert controller.wait_ready(page, {})
    assert controller.metrics[-1]['frames'] == 6


def test_matching_marks_with_changing_grid_are_not_ready(monkeypatch):
    from captcha import interactions
    clock = [0.0]
    monkeypatch.setattr(interactions.time, 'monotonic', lambda: clock[0])
    light, dark = io.BytesIO(), io.BytesIO()
    widget({2, 5}).save(light, format='PNG')
    image = widget({2, 5})
    ImageDraw.Draw(image).rectangle((10, 250, 390, 500), fill='black')
    image.save(dark, format='PNG')
    payloads = [light.getvalue(), dark.getvalue()]
    count = [0]
    def screenshot(**kwargs):
        clock[0] += .4
        count[0] += 1
        return payloads[count[0] % 2]
    monkeypatch.setattr(interactions, 'selection_marks', lambda *args: {2, 5})
    page = Mock()
    page.screenshot.side_effect = screenshot
    page.wait_for_timeout.side_effect = lambda ms: None
    controller = DynamicInteractions(timeout_ms=1500)
    controller.prepare(widget(), GRID, [2, 5])
    assert not controller.wait_ready(page, {})


def test_short_cpf_waits_for_value_and_button_without_changing_typing(monkeypatch):
    monkeypatch.setattr(session.random,'randint',lambda a,b:(a+b)//2)
    page=Mock()
    field,button=Mock(),Mock()
    field.input_value.return_value='000.000.000-00'
    field.bounding_box.return_value=button.bounding_box.return_value={'x':0,'y':0,'width':100,'height':30}
    button.is_enabled.side_effect=[False,True]
    page.locator.side_effect=lambda selector:Mock(first=button if selector.startswith('button') else field)
    session.submit_cpf(page,'000.000.000-00',short_pauses=True)
    assert page.keyboard.type.call_count==14
    assert all(c.kwargs['delay']==155 for c in page.keyboard.type.call_args_list)
    assert [c.args[0] for c in page.wait_for_timeout.call_args_list]==[150,150,50,150]
    assert [c.kwargs['steps'] for c in page.mouse.move.call_args_list]==[25,30]


def test_incomplete_cpf_is_not_submitted(monkeypatch):
    monkeypatch.setattr(session,'human_type',Mock())
    page=Mock()
    page.locator.return_value.first.input_value.return_value='000'
    with pytest.raises(RuntimeError,match='cpf_input_not_ready'):
        session.submit_cpf(page,'000.000.000-00',short_pauses=True)
    page.mouse.click.assert_not_called()
