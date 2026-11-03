"""Optional screenshot-based selection confirmation; no captcha iframe DOM access."""
import io
import random
import time

from PIL import Image, ImageChops, ImageStat
from .extractor import split_tiles


def _teal(pixel):
    r,g,b=pixel
    return r<25 and 100<=g<=170 and 110<=b<=180 and abs(g-b)<35


def selection_marks(before, after, grid):
    if before.size != after.size:
        return set()
    before,after=before.convert('RGB'),after.convert('RGB')
    gx,gy,gw,gh=grid
    cw,ch=gw/3,gh/3
    marks=set()
    for index in range(9):
        col,row=index%3,index//3
        x0,y0=int(gx+(col+.6)*cw),int(gy+row*ch)
        x1,y1=min(after.width,int(gx+(col+1)*cw)),min(after.height,int(gy+(row+.42)*ch))
        pixels={(x,y) for y in range(y0,y1) for x in range(x0,x1) if _teal(after.getpixel((x,y)))}
        while pixels:
            component={pixels.pop()}
            pending=list(component)
            while pending:
                x,y=pending.pop()
                for point in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                    if point in pixels:
                        pixels.remove(point)
                        component.add(point)
                        pending.append(point)
            if len(component)<40:
                continue
            left,right=min(x for x,y in component),max(x for x,y in component)
            top,bottom=min(y for x,y in component),max(y for x,y in component)
            if not (.07*cw<=right-left<=.24*cw and .07*ch<=bottom-top<=.24*ch):
                continue
            changed=sum(not _teal(before.getpixel(point)) for point in component)
            white=sum(min(after.getpixel((x,y)))>220 for y in range(top,bottom+1) for x in range(left,right+1))
            if changed>=20 and white>=3:
                marks.add(index+1)
                break
    return marks


class DynamicInteractions:
    def __init__(self, *, short_clicks=False, timeout_ms=2500, stable_ms=120):
        self.short_clicks=short_clicks
        self.timeout_ms=timeout_ms
        self.stable_ms=stable_ms
        self.metrics=[]

    def images_ready(self, crop, grid):
        # The calibrated 3x3 widget has instruction text above the grid.
        end_y=max(17,min(104,grid[1]-16))
        instruction=crop.convert('RGB').crop((16,16,max(17,int(crop.width*.6)),end_y))
        if sum(min(pixel)>220 for pixel in instruction.getdata())<30:
            return False
        # Gray loading tiles in the observed widget have fewer than 32 colors.
        return all(len(tile.convert('RGB').resize((32,32)).getcolors(1024))>=64
                   for tile in split_tiles(crop,grid))

    def prepare(self, before, grid, selected):
        self.before=before.copy()
        self.grid=grid
        self.selected=set(selected)

    def click(self, page, x, y):
        if not self.short_clicks:
            from .hcaptcha import click
            return click(page,x,y)
        page.mouse.move(x+random.uniform(-3,3),y+random.uniform(-3,3),steps=12)
        page.wait_for_timeout(random.randint(80,120))
        page.mouse.click(x,y)
        page.wait_for_timeout(random.randint(100,160))

    def wait_ready(self, page, clip):
        started=time.monotonic()
        stable_since=None
        previous=None
        confirmed=False
        frames=0
        while (time.monotonic()-started)*1000<self.timeout_ms:
            current=Image.open(io.BytesIO(page.screenshot(clip=clip,timeout=2000))).convert('RGB')
            frames+=1
            if selection_marks(self.before,current,self.grid)==self.selected:
                gx,gy,gw,gh=self.grid
                area=(gx,gy,gx+gw,gy+gh)
                delta=(sum(ImageStat.Stat(ImageChops.difference(previous.crop(area),current.crop(area))).mean)/3
                       if previous is not None and previous.size==current.size else float('inf'))
                if delta<.5:
                    if stable_since is None:
                        stable_since=time.monotonic()
                    elapsed=(time.monotonic()-started)*1000
                    if elapsed>=180 and (time.monotonic()-stable_since)*1000>=self.stable_ms:
                        confirmed=True
                        break
                else:
                    stable_since=None
            else:
                stable_since=None
            previous=current
            page.wait_for_timeout(60)
        self.metrics.append({'pre_submit_wait_ms':round((time.monotonic()-started)*1000),
            'confirmed':confirmed,'expected_selection_count':len(self.selected),'frames':frames})
        return confirmed
