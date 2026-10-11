"""Real image pixels: large preview, native/resized crops, orientation and caps."""
import io
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from PIL import Image
import nth_media as media

def encode(im, fmt='PNG', **kw):
    out = io.BytesIO(); im.save(out, format=fmt, **kw); return out.getvalue()

original = Image.new('RGB', (12000, 300), 'white')
original.paste((255, 0, 0), (11900, 100, 12000, 200))
raw = encode(original)
preview, mime, info = media.prepare_model_image(raw)
assert info['original_dimensions'] == [12000, 300]
assert info['output_dimensions'] == [2000, 50] and info['resized']
assert media.model_image_refusal(preview) is None
crop, _, native = media.prepare_model_image(raw, [11900, 100, 100, 100], False)
assert not native['resized'] and native['original_sha256'] == info['original_sha256']
assert Image.open(io.BytesIO(crop)).getpixel((50, 50)) == (255, 0, 0, 255)
resized, _, meta = media.prepare_model_image(raw, [0, 0, 12000, 200], True)
assert meta['output_dimensions'][0] == 2000
for box in [[0, 0, 12000, 200], [-1, 0, 1, 1], [11999, 0, 2, 1], [0, 0, 0, 1], [True, 0, 1, 1]]:
    try: media.prepare_model_image(raw, box, False)
    except ValueError: pass
    else: raise AssertionError(box)
# EXIF orientation is applied BEFORE original-coordinate cropping.
photo = Image.new('RGB', (60, 30), 'red'); photo.paste('blue', (30, 0, 60, 30))
exif = Image.Exif(); exif[274] = 6
jpg = encode(photo, 'JPEG', exif=exif, quality=100, subsampling=0)
result, _, meta = media.prepare_model_image(jpg, [0, 30, 30, 30], False)
assert meta['original_dimensions'] == [30, 60]
assert Image.open(io.BytesIO(result)).getpixel((15, 15))[2] > 240
png_exif = encode(photo, exif=exif)
_, _, png_meta = media.prepare_model_image(png_exif)
assert png_meta['original_dimensions'] == [30, 60]
assert raw == encode(original)
try: media.prepare_model_image(b'x' * (media.MAX_MODEL_SOURCE_BYTES + 1))
except ValueError as e: assert str(e) == 'source_too_large'
else: raise AssertionError('byte cap')
# Native RGBA crop retains transparency without lossy encoding.
a = Image.new('RGBA', (2200, 10), (1, 2, 3, 77))
data, _, _ = media.prepare_model_image(encode(a), [0, 0, 20, 10], False)
assert Image.open(io.BytesIO(data)).getpixel((0, 0)) == (1, 2, 3, 77)
print('PASS: 12000px previews, 1:1 pixels, resized crops, bounds, EXIF, alpha, immutable originals')

# Exercise the actual tool, including an invisible DM crop and no mutation.
import base64
import json
import os
import tempfile
with tempfile.TemporaryDirectory(prefix='image_preview_') as home:
    os.environ['NTH_HOME'] = home
    os.environ['NTH_QUIET'] = '1'
    import nth_server as srv
    from nth_constants import AGENT_INBOX_CHANNEL
    def join(name):
        return json.loads(srv.nth_connect(summary='image test', name=name, channel='crop-test'))
    a, b, c = [join(n) for n in ['Alpha', 'Beta', 'Gamma']]
    sent = json.loads(srv.nth_send(channel='crop-test', member_id=a['member_id'],
        session_token=a['session_token'], message='large image',
        attachments=[{'data_base64': base64.b64encode(raw).decode(), 'filename': 'large-inspection-strip.png'}]))
    aid = sent['attachments'][0]['id']
    out = srv.nth_image('crop-test', b['member_id'], aid, b['session_token'], [11900,100,100,100], False)
    assert isinstance(out, list) and json.loads(out[0])['output_dimensions'] == [100,100]
    assert json.loads(out[0])['original_sha256'] == info['original_sha256']
    assert 'error' in json.loads(srv.nth_image('crop-test', b['member_id'], aid, 'invalid', [0,0,1,1]))
    assert 'error' in json.loads(srv.nth_image('crop-test', 'outsider', aid, crop=[0,0,1,1]))
    print('PASS: actual tool native crop, original hash, invalid-session and outsider rejection')
    dm = json.loads(srv.nth_dm(member_id=a['member_id'], to='Beta', message='private crop',
        session_token=a['session_token'], attachments=[{'data_base64': base64.b64encode(raw).decode(),
        'filename': 'private-inspection-strip.png'}]))
    did = dm['attachments'][0]['id']
    assert isinstance(srv.nth_image(AGENT_INBOX_CHANNEL, b['member_id'], did, crop=[0,0,10,10], resize=False), list)
    denied = srv.nth_image(AGENT_INBOX_CHANNEL, c['member_id'], did, crop=[0,0,10,10])
    assert json.loads(denied)['reason'] == 'not_found'
    print('PASS: native private crop visible only to DM participants')
