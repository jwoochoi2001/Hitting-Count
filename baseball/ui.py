"""버튼, 텍스트 입력 등 간단한 UI 위젯."""
import pygame

from . import config as C
from .assets import get_font


def draw_text(surface, text, size, x, y, color=C.WHITE, center=False,
              bold=False, right=False):
    """텍스트를 그리고 사용된 rect 를 돌려준다."""
    font = get_font(size, bold=bold)
    img = font.render(text, True, color)
    rect = img.get_rect()
    if center:
        rect.center = (x, y)
    elif right:
        rect.midright = (x, y)
    else:
        rect.topleft = (x, y)
    surface.blit(img, rect)
    return rect


class Button:
    def __init__(self, rect, text, size=30, color=C.PANEL_LIGHT,
                 text_color=C.WHITE, hover=C.ACCENT):
        self.rect = pygame.Rect(rect)
        self.text = text
        self.size = size
        self.color = color
        self.text_color = text_color
        self.hover = hover
        self.enabled = True
        self._hovered = False

    def draw(self, surface):
        base = self.color
        if not self.enabled:
            base = C.GRAY
        elif self._hovered:
            base = self.hover
        pygame.draw.rect(surface, base, self.rect, border_radius=12)
        pygame.draw.rect(surface, C.BLACK, self.rect, width=2, border_radius=12)
        draw_text(surface, self.text, self.size, self.rect.centerx,
                  self.rect.centery, self.text_color, center=True, bold=True)

    def update(self, mouse_pos):
        self._hovered = self.enabled and self.rect.collidepoint(mouse_pos)

    def clicked(self, event):
        if not self.enabled:
            return False
        return (event.type == pygame.MOUSEBUTTONDOWN and event.button == 1
                and self.rect.collidepoint(event.pos))


class TextInput:
    """클릭하면 활성화되고 키보드로 텍스트를 편집하는 입력창.

    처음 생성될 때의 글자("플레이어1" 같은 기본값)는 선택하는 순간 자동으로
    지워져 바로 새 글자를 쓸 수 있고, 아무것도 안 쓰고 벗어나면 그 기본값이
    다시 채워진다(비워두면 어차피 그 기본값으로 경기가 시작되므로).
    """

    def __init__(self, rect, text="", size=26, max_len=8, default=None):
        self.rect = pygame.Rect(rect)
        self.text = text
        # _initial: 처음 채워진 글자(선택하면 지워짐), _default: 비워두고 벗어났을
        # 때 다시 채우는 글자(따로 안 주면 처음 글자와 같다)
        self._initial = text
        self._default = text if default is None else default
        self.size = size
        self.max_len = max_len
        self.active = False
        # 한글 등 IME 로 조합 중인(아직 확정 안 된) 글자 미리보기
        self.composing = ""

    def activate(self):
        if self.active:
            return
        self.active = True
        if self.text in (self._initial, self._default):
            self.text = ""

    def deactivate(self):
        if not self.active:
            return
        self.active = False
        self.composing = ""
        if self.text.strip() == "":
            self.text = self._default

    def handle(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self.rect.collidepoint(event.pos):
                self.activate()
            else:
                self.deactivate()
        elif event.type == pygame.KEYDOWN and self.active:
            if event.key == pygame.K_BACKSPACE:
                self.text = self.text[:-1]
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
                self.deactivate()
        elif event.type == pygame.TEXTEDITING and self.active:
            # IME 조합 중인 글자(아직 완성 전) — 확정되기 전에도 화면에 즉시 보이도록.
            self.composing = event.text
        elif event.type == pygame.TEXTINPUT and self.active:
            # TEXTINPUT 은 한글 등 IME 로 조합된 완성 문자를 전달한다(KEYDOWN.unicode 는 조합 전 입력이라 한글 입력이 안 됨).
            self.composing = ""
            # Tab 등 제어문자는 무시(Tab 은 입력창 이동에 쓰인다).
            if event.text and event.text != "\t" and len(self.text) < self.max_len:
                self.text += event.text

    def draw(self, surface):
        border = C.ACCENT2 if self.active else C.BLACK
        pygame.draw.rect(surface, C.WHITE, self.rect, border_radius=8)
        pygame.draw.rect(surface, border, self.rect, width=3, border_radius=8)
        shown = self.text + self.composing
        draw_text(surface, shown, self.size, self.rect.centerx,
                  self.rect.centery, C.BLACK, center=True)


class Dropdown:
    """클릭하면 아래로(자리가 없으면 위로) 긴 목록이 펼쳐지고 하나를 고르는 선택 상자.

    fixed=True 면 값을 바꿀 수 없는 고정 표시(예: 투수 칸의 P)."""

    ROW_H = 26

    def __init__(self, rect, options, value, size=17, fixed=False, row_h=None):
        self.rect = pygame.Rect(rect)
        if row_h:
            self.ROW_H = row_h
        self.options = list(options)
        self.value = value
        self.size = size
        self.fixed = fixed
        self.open = False
        self._hi = 0          # 키보드로 고르는 중인 항목 번호
        self._hovered = False

    def list_rect(self):
        h = len(self.options) * self.ROW_H + 4
        y = self.rect.bottom + 2
        if y + h > C.HEIGHT - 6:
            y = max(6, self.rect.top - h - 2)
        return pygame.Rect(self.rect.x, y, max(self.rect.w, 64), h)

    def update(self, mouse_pos):
        self._hovered = (not self.fixed) and self.rect.collidepoint(mouse_pos)

    def handle(self, event):
        """이벤트를 처리하고, 값이 바뀌면 True 를 돌려준다."""
        if self.fixed:
            return False
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self.open:
                lr = self.list_rect()
                self.open = False
                if lr.collidepoint(event.pos):
                    idx = (event.pos[1] - lr.y - 2) // self.ROW_H
                    if 0 <= idx < len(self.options):
                        changed = self.options[idx] != self.value
                        self.value = self.options[idx]
                        return changed
            elif self.rect.collidepoint(event.pos):
                self.open = True
                self._hi = (self.options.index(self.value)
                            if self.value in self.options else 0)
        elif event.type == pygame.KEYDOWN and self.open:
            if event.key == pygame.K_ESCAPE:
                self.open = False
            elif event.key == pygame.K_DOWN:
                self._hi = (self._hi + 1) % len(self.options)
            elif event.key == pygame.K_UP:
                self._hi = (self._hi - 1) % len(self.options)
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.open = False
                changed = self.options[self._hi] != self.value
                self.value = self.options[self._hi]
                return changed
        return False

    def draw(self, surface):
        if self.fixed:
            color = (52, 56, 70)
        elif self.open or self._hovered:
            color = C.ACCENT
        else:
            color = C.PANEL_LIGHT
        pygame.draw.rect(surface, color, self.rect, border_radius=8)
        pygame.draw.rect(surface, C.BLACK, self.rect, width=2, border_radius=8)
        label_x = self.rect.centerx - (0 if self.fixed else 6)
        draw_text(surface, self.value, self.size, label_x, self.rect.centery,
                  C.WHITE, center=True, bold=True)
        if not self.fixed:
            ax, ay = self.rect.right - 11, self.rect.centery
            pygame.draw.polygon(surface, C.WHITE,
                                [(ax - 4, ay - 2), (ax + 4, ay - 2), (ax, ay + 3)])

    def draw_list(self, surface):
        """펼쳐진 목록(다른 위젯 위에 보이도록 마지막에 따로 그린다)."""
        if not self.open:
            return
        lr = self.list_rect()
        pygame.draw.rect(surface, (18, 22, 32), lr, border_radius=8)
        pygame.draw.rect(surface, C.ACCENT2, lr, width=2, border_radius=8)
        mouse = pygame.mouse.get_pos()
        for i, opt in enumerate(self.options):
            row = pygame.Rect(lr.x + 3, lr.y + 2 + i * self.ROW_H, lr.w - 6,
                              self.ROW_H)
            hot = row.collidepoint(mouse) or (i == self._hi and not lr.collidepoint(mouse))
            if hot:
                pygame.draw.rect(surface, C.ACCENT, row, border_radius=5)
            elif opt == self.value:
                pygame.draw.rect(surface, (44, 50, 66), row, border_radius=5)
            draw_text(surface, opt, self.size, row.centerx, row.centery,
                      C.WHITE, center=True, bold=(opt == self.value))
