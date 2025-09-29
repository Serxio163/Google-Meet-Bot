"""
Service module for Yandex Telemost (Яндекс Телемост) meetings.
"""
import base64
import logging
import random
import time
import urllib.parse
from typing import Optional

from selenium.common.exceptions import (
    ElementNotInteractableException,
    InvalidSessionIdException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from ..base import MeetingService
from ..exceptions import CaptchaDetectedException

logger = logging.getLogger(__name__)


class YandexTelemostService(MeetingService):
    """Service for Yandex Telemost (Яндекс Телемост) meetings."""

    def __init__(self, driver, mail_address: Optional[str] = None, password: Optional[str] = None, guest_name: str = "Гость"):
        super().__init__(driver)
        self.mail_address = mail_address
        self.password = password
        self.guest_name = guest_name

    def _is_captcha_page(self) -> bool:
        """Проверить, попали ли мы на страницу с капчей."""
        try:
            current_url = self.driver.current_url
            
            # Проверка по URL
            if any(keyword in current_url.lower() for keyword in [
                'showcaptcha', 'captcha', 'smartcaptcha', 'captcha.yandex.ru'
            ]):
                logger.info("CAPTCHA detected by URL keywords")
                return True
            
            # Расширенный список селекторов для капчи
            captcha_selectors = [
                # Яндекс SmartCaptcha
                "div[data-captcha-type='smartcaptcha']",
                "div[class*='smartcaptcha']",
                "div[id*='smartcaptcha']",
                "img[src*='smartcaptcha']",
                
                # Общие селекторы капчи
                "img[src*='captcha']",
                "img[alt*='captcha']",
                "div[class*='captcha']",
                "div[id*='captcha']",
                "input[name*='captcha']",
                "textarea[name*='captcha']",
                
                # Яндекс-специфичные
                "div[class*='CheckboxCaptcha']",
                "div[class*='ImageCaptcha']",
                "button[class*='captcha__submit']",
                "input[class*='captcha__input']",
                
                # Контейнеры и изображения
                "div.captcha",
                "#captcha",
                ".captcha-image",
                ".captcha-container",
                
                # Текстовые индикаторы
                "//*[contains(text(), 'Введите код с картинки')]",
                "//*[contains(text(), 'Подтвердите, что вы не робот')]",
                "//*[contains(text(), 'Докажите, что вы человек')]",
                "//*[contains(text(), 'captcha')]",
                "//*[contains(text(), 'капча')]",
                "//*[contains(text(), 'Капча')]",
                
                # Поля ввода капчи
                "//input[@placeholder and (contains(@placeholder, 'код') or contains(@placeholder, 'символы'))]",
                "//input[@aria-label and contains(@aria-label, 'captcha')]",
                
                # Кнопки и ссылки
                "//button[contains(text(), 'Проверить')]",
                "//button[contains(text(), 'Отправить')]",
                "//a[contains(text(), 'Обновить картинку')]",
                
                # Дополнительные селекторы для Yandex
                "div[data-captcha]",
                "div[data-captcha-id]",
                "script[src*='captcha']",
            ]
            
            # Проверяем каждый селектор
            for selector in captcha_selectors:
                try:
                    if selector.startswith("//") or selector.startswith("(//"):
                        # XPath селектор
                        elements = self.driver.find_elements(By.XPATH, selector)
                    else:
                        # CSS селектор
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    
                    for element in elements:
                        if element.is_displayed():
                            logger.info(f"CAPTCHA element found with selector: {selector}")
                            return True
                            
                except Exception as e:
                    logger.debug(f"Selector {selector} check failed: {e}")
                    continue
            
            # Дополнительная проверка через JavaScript
            try:
                js_check = self.driver.execute_script("""
                    // Проверяем наличие капчи через различные методы
                    var captchaKeywords = ['captcha', 'капча', 'smartcaptcha'];
                    var elements = document.querySelectorAll('*');
                    
                    for (var i = 0; i < elements.length; i++) {
                        var el = elements[i];
                        var text = (el.textContent || '').toLowerCase();
                        var className = (el.className || '').toLowerCase();
                        var id = (el.id || '').toLowerCase();
                        
                        for (var j = 0; j < captchaKeywords.length; j++) {
                            var keyword = captchaKeywords[j];
                            if (text.includes(keyword) || className.includes(keyword) || id.includes(keyword)) {
                                if (el.offsetWidth > 0 && el.offsetHeight > 0) {
                                    return true;
                                }
                            }
                        }
                    }
                    
                    // Проверяем изображения на наличие captcha в src
                    var images = document.querySelectorAll('img');
                    for (var i = 0; i < images.length; i++) {
                        var src = images[i].src || '';
                        if (src.toLowerCase().includes('captcha')) {
                            return true;
                        }
                    }
                    
                    return false;
                """)
                
                if js_check:
                    logger.info("CAPTCHA detected by JavaScript analysis")
                    return True
                    
            except Exception as e:
                logger.debug(f"JavaScript CAPTCHA check failed: {e}")
            
            logger.debug("No CAPTCHA detected")
            return False
            
        except Exception as e:
            logger.error(f"Error checking for CAPTCHA: {e}")
            return False

    def _handle_captcha(self) -> None:
        """Обработать капчу с использованием расширенных стратегий обхода."""
        try:
            logger.info("Starting enhanced CAPTCHA bypass...")
            
            # Сделаем скриншот для отладки
            try:
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                screenshot_name = f"captcha_detected_{timestamp}.png"
                self.driver.save_screenshot(screenshot_name)
                logger.info(f"Saved CAPTCHA screenshot: {screenshot_name}")
            except Exception as e:
                logger.warning(f"Failed to save CAPTCHA screenshot: {e}")
            
            # Стратегия 1: Попытка обновить страницу с разными интервалами
            for attempt in range(1, 4):
                logger.info(f"CAPTCHA bypass attempt {attempt}/5")
                
                # Обновляем страницу с разными интервалами
                wait_time = random.uniform(2, 6)
                logger.info(f"Refreshing page, waiting {wait_time:.1f} seconds...")
                self.driver.refresh()
                time.sleep(wait_time)
                
                if not self._is_captcha_page():
                    logger.info("CAPTCHA bypassed by page refresh!")
                    return
            
            # Стратегия 2: Поиск и клик по элементам обхода
            bypass_selectors = [
                # Кнопки продолжения
                "button[type='submit']",
                "button:not([disabled])",
                "input[type='submit']",
                "input[type='button']",
                
                # Текстовые кнопки
                "//button[contains(text(), 'Продолжить')]",
                "//button[contains(text(), 'Пропустить')]",
                "//button[contains(text(), 'Далее')]",
                "//button[contains(text(), 'Войти')]",
                "//button[contains(text(), 'Enter')]",
                
                // Ссылки
                "//a[contains(text(), 'Продолжить')]",
                "//a[contains(text(), 'Пропустить')]",
                "//a[contains(text(), 'Далее')]",
                
                // Специфичные классы
                "button[class*='continue']",
                "button[class*='skip']",
                "button[class*='next']",
                "button[class*='submit']",
                
                // Яндекс-специфичные
                "button[class*='CheckboxCaptcha-Button']",
                "button[class*='captcha__submit']",
                
                // Любые кликабельные элементы
                "div[role='button']",
                "span[role='button']",
                "a[href='#']",
                "a[href='javascript:void(0)']",
            ]
            
            for sel_value in bypass_selectors:
                try:
                    if sel_value.startswith("//"):
                        elements = self.driver.find_elements(By.XPATH, sel_value)
                    else:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, sel_value)
                    
                    for element in elements:
                        if element.is_displayed() and element.is_enabled():
                            logger.info(f"Attempting to click bypass element: {sel_value}")
                            
                            # Прокручиваем элемент в видимую область
                            self.driver.execute_script("arguments[0].scrollIntoView(true);", element)
                            time.sleep(0.5)
                            
                            // Используем JavaScript для клика
                            self.driver.execute_script("arguments[0].click();", element)
                            logger.info(f"Clicked bypass element: {sel_value}")
                            time.sleep(3)
                            
                            if not self._is_captcha_page():
                                logger.info("CAPTCHA bypassed by clicking element!")
                                return
                            break
                except Exception as e:
                    logger.debug(f"Bypass selector {sel_value} failed: {e}")
                    continue
            
            // Стратегия 3: Продвинутый JavaScript для обхода
            try:
                // Several different JavaScript approaches
                js_scripts = [
                    // Hide captcha and click first available button
                    """
                    // Hide captcha elements
                    var captchaElements = document.querySelectorAll('[data-captcha], img[src*="captcha"], div[class*="captcha"]');
                    captchaElements.forEach(function(el) {
                        el.style.display = 'none';
                        el.style.visibility = 'hidden';
                    });
                    
                    // Find and click first available button
                    var buttons = document.querySelectorAll('button:not([disabled]), input[type="submit"]:not([disabled])');
                    for(var i = 0; i < buttons.length; i++) {
                        var btn = buttons[i];
                        if (btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                            btn.click();
                            break;
                        }
                    }
                    """,
                    
                    // Try to bypass captcha form
                    """
                    // Look for form and try to submit it
                    var forms = document.querySelectorAll('form');
                    forms.forEach(function(form) {
                        if (form.action && form.action.includes('captcha')) {
                            form.submit();
                        }
                    });
                    """,
                    
                    // Remove captcha elements from DOM
                    """
                    // Remove captcha elements completely
                    var captchaElements = document.querySelectorAll('div[class*="captcha"], img[src*="captcha"], form[action*="captcha"]');
                    captchaElements.forEach(function(el) {
                        el.parentNode.removeChild(el);
                    });
                    """
                ]
                
                for i, script in enumerate(js_scripts):
                    try:
                        self.driver.execute_script(script)
                        logger.info(f"Executed JavaScript bypass attempt {i+1}")
                        time.sleep(3)
                        
                        if not self._is_captcha_page():
                            logger.info("CAPTCHA bypassed by JavaScript!")
                            return
                            
                    except Exception as e:
                        logger.debug(f"JavaScript bypass {i+1} failed: {e}")
                        continue
                    
            except Exception as e:
                logger.debug(f"JavaScript bypass failed: {e}")
            
            // Стратегия 4: Попытка перейти по прямой ссылке, обходя капчу
            try:
                current_url = self.driver.current_url
                if "showcaptcha" in current_url:
                    // Попробуем извлечь исходную ссылку из параметра retpath
                    import urllib.parse
                    import base64
                    
                    parsed_url = urllib.parse.urlparse(current_url)
                    params = urllib.parse.parse_qs(parsed_url.query)
                    
                    if 'retpath' in params:
                        retpath_value = params['retpath'][0]
                        logger.info(f"Found retpath parameter: {retpath_value}")
                        
                        // Пробуем разные методы декодирования
                        try:
                            // Попытка URL декодирования
                            original_url = urllib.parse.unquote(retpath_value)
                            logger.info(f"Attempting URL decode: {original_url}")
                            self.driver.get(original_url)
                            time.sleep(5)
                            
                            if not self._is_captcha_page():
                                logger.info("CAPTCHA bypassed by URL decode!")
                                return
                                
                        except Exception:
                            pass
                        
                        // Попытка base64 декодирования
                        try:
                            decoded_bytes = base64.b64decode(retpath_value + '==')
                            original_url = decoded_bytes.decode('utf-8')
                            logger.info(f"Attempting base64 decode: {original_url}")
                            self.driver.get(original_url)
                            time.sleep(5)
                            
                            if not self._is_captcha_page():
                                logger.info("CAPTCHA bypassed by base64 decode!")
                                return
                                
                        except Exception:
                            pass
                            
            except Exception as e:
                logger.debug(f"URL bypass failed: {e}")
            
            // Стратегия 5: Попытка использовать куки или локальное хранилище
            try:
                // Очищаем куки и перезагружаем страницу
                self.driver.delete_all_cookies()
                self.driver.refresh()
                time.sleep(5)
                
                if not self._is_captcha_page():
                    logger.info("CAPTCHA bypassed by clearing cookies!")
                    return
                    
            except Exception as e:
                logger.debug(f"Cookie bypass failed: {e}")
            
            logger.warning("All automatic CAPTCHA bypass strategies failed. CAPTCHA still present.")
            
        except Exception as e:
            logger.error(f"Error handling CAPTCHA: {e}")

    def _fill_guest_name_if_present(self) -> None:
        """Заполнить имя гостя, если поле доступно."""
        try:
            logger.info("Looking for guest name input field...")
            
            // Попробуем найти поле ввода имени гостя по различным селекторам
            guest_name_selectors = [
                "input[placeholder*='имя']",
                "input[placeholder*='name']",
                "input[placeholder*='Name']",
                "input[type='text']",
                "input[name*='name']",
                "input[name*='Name']",
                "input[id*='name']",
                "input[id*='Name']",
                "input[class*='name']",
                "input[class*='Name']"
            ]
            
            // Сначала попробуем найти любые видимые поля ввода
            all_inputs = self.driver.find_elements(By.TAG_NAME, "input")
            logger.info(f"Found {len(all_inputs)} input elements on page")
            
            for i, input_elem in enumerate(all_inputs):
                if input_elem.is_displayed():
                    logger.info(f"Input {i}: type={input_elem.get_attribute('type')}, placeholder={input_elem.get_attribute('placeholder')}, name={input_elem.get_attribute('name')}, id={input_elem.get_attribute('id')}")
            
            for selector in guest_name_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for element in elements:
                        if element.is_displayed():
                            element.clear()
                            element.send_keys(self.guest_name)
                            logger.info(f"Guest name filled using selector: {selector}")
                            time.sleep(1)
                            return
                except Exception as e:
                    logger.debug(f"Selector {selector} failed: {e}")
                    continue
                    
            logger.info("Guest name input field not found - this might be normal")
            
        except Exception as e:
            logger.warning(f"Failed to fill guest name: {e}")

    def _handle_initial_page_scenarios(self) -> None:
        """Обработать различные сценарии на начальной странице."""
        try:
            current_url = self.driver.current_url
            logger.info(f"Handling initial page scenarios for URL: {current_url}")
            
            // Если мы на странице регистрации, пробуем вернуться к встрече
            if any(keyword in current_url.lower() for keyword in ['registration', 'signup', 'register']):
                logger.warning("On registration page! Attempting to navigate back to meeting...")
                
                // Ищем кнопки возврата или продолжения как гость
                return_selectors = [
                    "//button[contains(text(), 'Продолжить как гость')]",
                    "//button[contains(text(), 'Continue as guest')]",
                    "//a[contains(text(), 'Вернуться')]",
                    "//a[contains(text(), 'Back')]",
                    "button[class*='guest']",
                    "a[class*='guest']",
                ]
                
                for selector in return_selectors:
                    try:
                        if selector.startswith("//"):
                            elements = self.driver.find_elements(By.XPATH, selector)
                        else:
                            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                        
                        for element in elements:
                            if element.is_displayed() and element.is_enabled():
                                self.driver.execute_script("arguments[0].click();", element)
                                logger.info(f"Clicked return element: {selector}")
                                time.sleep(3)
                                return
                    except Exception as e:
                        logger.debug(f"Return selector {selector} failed: {e}")
                        continue
            
            // Проверяем, не требуется ли подтверждение
            confirmation_selectors = [
                "//button[contains(text(), 'Подтвердить')]",
                "//button[contains(text(), 'Confirm')]",
                "//button[contains(text(), 'ОК')]",
                "//button[contains(text(), 'Ok')]",
            ]
            
            for selector in confirmation_selectors:
                try:
                    if selector.startswith("//"):
                        elements = self.driver.find_elements(By.XPATH, selector)
                    else:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    
                    for element in elements:
                        if element.is_displayed() and element.is_enabled():
                            self.driver.execute_script("arguments[0].click();", element)
                            logger.info(f"Clicked confirmation element: {selector}")
                            time.sleep(2)
                            return
                except Exception as e:
                    logger.debug(f"Confirmation selector {selector} failed: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"Error handling initial page scenarios: {e}")

    def login(self, email: Optional[str] = None, password: Optional[str] = None) -> None:
        """Авторизация в Яндекс ID через passport.yandex.ru."""
        // Debug logging to see what credentials are being used
        logger.info(f"Login called with email: {email}, password: {'[REDACTED]' if password else 'None'}")
        logger.info(f"Self.mail_address: {self.mail_address}, self.password: {'[REDACTED]' if self.password else 'None'}")

        // Completely skip authorization if no credentials provided
        if not email or not password:
            logger.info("Yandex login skipped: using guest flow (no credentials provided)")
            return
            
        // Use provided credentials or fall back to instance variables
        mail = email or self.mail_address
        pwd = password or self.password

        // Only attempt authorization if credentials are provided
        logger.info("Attempting Yandex authorization with provided credentials")
        try:
            self.driver.get("https://passport.yandex.ru/auth")
            // Ввод логина
            try:
                login_input = WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.NAME, "login"))
                )
                logger.debug("Login input found using primary selector: By.NAME, 'login'")
                login_input.clear()
                login_input.send_keys(mail)
            except Exception as e:
                logger.warning("Primary login selector failed: %s", e)
                try:
                    alt_login = WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='login']"))
                    )
                    logger.debug("Login input found using alternative selector: By.CSS_SELECTOR, 'input[name=login]'")
                    alt_login.send_keys(mail)
                except Exception as e:
                    logger.error("Both login selectors failed: %s", e)

            try:
                passwd_input = WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.NAME, "passwd"))
                )
                logger.debug("Password input found using primary selector: By.NAME, 'passwd'")
                passwd_input.clear()
                passwd_input.send_keys(pwd)
            except Exception as e:
                logger.warning("Primary password selector failed: %s", e)
                try:
                    alt_pass = WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='passwd']"))
                    )
                    logger.debug("Password input found using alternative selector: By.CSS_SELECTOR, 'input[name=passwd]'")
                    alt_pass.send_keys(pwd)
                except Exception as e:
                    logger.error("Both password selectors failed: %s", e)

            # Click "Sign In" (first step)
            self._click_first_available([
                ("css", "button#passp\\:sign-in"),
                ("xpath", "//button[contains(@id,'passp:sign-in')"),
                ("xpath", "//button[.//span[contains(text(),'Войти')]]"),
            ], timeout=10)

            # Password input
            try:
                passwd_input = WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.NAME, "passwd"))
                )
                passwd_input.clear()
                passwd_input.send_keys(pwd)
            except Exception:
                alt_pass = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='passwd']"))
                )
                alt_pass.send_keys(pwd)

            # Click "Sign In" (second step)
            self._click_first_available([
                ("css", "button#passp\\:sign-in"),
                ("xpath", "//button[contains(@id,'passp:sign-in')"),
                ("xpath", "//button[.//span[contains(text(),'Войти')]]"),
            ], timeout=10)

            # Normalize session
            logger.debug("Removed implicit wait; relying on explicit waits")
            logger.info("Yandex login activity: Done")
        except TimeoutException as e:
            logger.warning("Yandex login failed due to timeout; continuing as guest. Details: %s", e)
        except Exception as e:
            logger.warning("Yandex login error; continuing as guest. Details: %s", e)

    def pre_join_setup(self, meeting_link: str) -> None:
        """Открыть ссылку встречи и выключить микрофон/камеру на экране предварительного подключения."""
        if not meeting_link:
            raise RuntimeError("Ссылка на встречу обязательна для Яндекс Телемост")
        
        logger.info(f"Starting pre_join_setup for meeting: {meeting_link}")
        
        // Убедимся, что сессия браузера активна
        self._ensure_session_alive(meeting_link)
        
        // Добавляем случайную задержку для имитации человеческого поведения
        time.sleep(random.uniform(2, 4))
        
        self.driver.get(meeting_link)
        logger.info(f"Navigated to meeting link: {meeting_link}")
        
        // Подождать загрузку первого экрана с увеличенным таймаутом
        time.sleep(12)  // Увеличено время ожидания для медленной загрузки
        
        // Сделаем скриншот для отладки
        try:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            screenshot_name = f"debug_pre_join_page_{timestamp}.png"
            self.driver.save_screenshot(screenshot_name)
            logger.info(f"Saved debug screenshot: {screenshot_name}")
            
            // Показываем текущий URL для диагностики
            current_url = self.driver.current_url
            logger.info(f"Current URL after navigation: {current_url}")
            
            // Показываем заголовок страницы
            page_title = self.driver.title
            logger.info(f"Page title: {page_title}")
            
            // Показываем весь HTML страницы для анализа
            try:
                page_source = self.driver.page_source
                logger.info(f"Page source length: {len(page_source)} characters")
                // Ищем ключевые элементы в HTML
                if "Продолжить в браузере" in page_source:
                    logger.info("Found 'Continue in browser' text in page source")
                if "имя" in page_source.lower():
                    logger.info("Found 'name' text in page source")
                if "войти" in page_source.lower():
                    logger.info("Found 'join' text in page source")
            except Exception as e:
                logger.warning(f"Failed to analyze page source: {e}")
            
        except Exception as e:
            logger.warning(f"Failed to save screenshot or get page info: {e}")
        
        // Проверяем, не попали ли мы на страницу с капчей
        if self._is_captcha_page():
            logger.warning("CAPTCHA detected! Attempting to handle...")
            self._handle_captcha()
            // ВАЖНО: Не выходим из метода! Продолжаем выполнение после обхода CAPTCHA
            logger.info("CAPTCHA handling completed, continuing with pre-join setup...")
        
        // Проверяем, не попали ли мы на страницу авторизации
        if "passport.yandex.ru" in self.driver.current_url:
            logger.warning("Redirected to login page! This shouldn't happen in guest mode.")
            // Возвращаемся назад к встрече
            self.driver.get(meeting_link)
            time.sleep(5)
        
        // Обрабатываем различные сценарии на странице
        self._handle_initial_page_scenarios()
    
        // Первый экран: «Продолжить в браузере»
        clicked_continue = self._click_first_available([
            ("xpath", "//button[contains(.,'Продолжить в браузере')]"),
            ("xpath", "//*[self::button or @role='button'][contains(.,'Продолжить в браузере')]"),
            ("xpath", "//a[contains(.,'Продолжить в браузере')]"),
            ("css", "[data-qa*='continueInBrowser'], [data-qa*='openInBrowser']"),
        ], timeout=5)
        // Если открылось новое окно/вкладка, переключимся на неё
        if clicked_continue:
            try:
                time.sleep(1)
                handles = self.driver.window_handles
                if len(handles) > 1:
                    self.driver.switch_to.window(handles[-1])
            except Exception:
                pass
    
        // Cookie/consent баннеры (пробуем закрыть, если есть)
        self._click_first_available([
            ("xpath", "//button[contains(.,'Понятно') or contains(.,'Принять') or contains(.,'Хорошо') or contains(.,'Ок') or contains(.,'ОК')]"),
        ], timeout=3)
    
        // Если есть поле ввода имени гостя — заполним
        try:
            name_input_selectors = [
                ("css", "input[name='name']"),
                ("css", "input[placeholder*='имя']"),
                ("xpath", "//input[contains(@placeholder,'имя') or contains(@aria-label,'имя')]")
            ]
            for sel_type, sel_value in name_input_selectors:
                try:
                    el = WebDriverWait(self.driver, 3).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel_value) if sel_type == "css" else (By.XPATH, sel_value)))
                    el.clear()
                    el.send_keys(self.guest_name)
                    logger.info("Guest name entered: %s", self.guest_name)
                    break
                except Exception:
                    logger.debug("Guest name input not found: %s", sel_value)
                    continue
        except Exception:
            logger.warning("Failed to enter guest name")
    
        // Выключить микрофон и камеру через улучшенный метод
        self._toggle_mic_cam_if_present()
    
        // Отладочная информация
        try:
            logger.debug("Current URL: %s", self.driver.current_url)
        except Exception:
            pass

    def join(self, meeting_link: str) -> None:
        """Непосредственно присоединиться к встрече (кнопка Подключиться/Присоединиться/Запросить доступ)."""
        try:
            // Страница уже должна быть открыта в pre_join_setup, но подстрахуемся
            if not self.driver.current_url.startswith("http"):
                self.driver.get(meeting_link)
        except Exception:
            try:
                self.driver.get(meeting_link)
            except Exception:
                pass
        
        // Дополнительное ожидание загрузки страницы
        time.sleep(3)
        
        // Сделаем скриншот для отладки
        try:
            self.driver.save_screenshot("debug_join_page.png")
            logger.debug("Saved debug screenshot: debug_join_page.png")
        except Exception as e:
            logger.debug("Failed to save screenshot: %s", e)
        
        // Попробуем найти и кликнуть кнопку присоединения
        joined_clicked = self._click_first_available([
            ("xpath", "//*[self::button or @role='button'][contains(.,'Подключиться') or contains(.,'Присоединиться') or contains(.,'Запросить доступ') or contains(.,'Войти')]"),
            ("css", "button[data-qa*='join'], [data-qa*='join']"),
            ("xpath", "//button[contains(.,'Ask to join') or contains(.,'Join')]"),
            ("xpath", "//button[contains(@class,'join')]"),
            ("css", "button[type='submit']"),
            ("xpath", "//button[not(@disabled)]"),
            ("xpath", "//button[contains(translate(text(), 'ВОЙТИ','войти'), 'войти')]"),
            ("xpath", "//button[contains(translate(@aria-label, 'ВОЙТИ','войти'), 'войти')]"),
            ("css", "button[aria-label*='войти'], button[title*='войти']"),
            ("css", "button[aria-label*='Join'], button[title*='Join']"),
            ("css", "button[aria-label*='присоединиться'], button[title*='присоединиться']"),
        ], timeout=25)
        
        if joined_clicked:
            logger.info("Ask/Join button click: Done (Yandex)")
            // Подождать немного после клика
            time.sleep(2)
        else:
            logger.warning("Join button not found or not clickable (Yandex)")
            // Попробуем нажать Enter на случай, если фокус на нужной кнопке
            try:
                from selenium.webdriver.common.keys import Keys
                self.driver.switch_to.active_element.send_keys(Keys.ENTER)
                logger.info("Tried pressing Enter as fallback")
                time.sleep(2)
            except Exception as e:
                logger.debug("Enter key fallback failed: %s", e)

    def wait_until_joined(self, timeout_sec: int = 60) -> bool:
        """Дождаться входа во встречу: исчезновение кнопки «Подключиться» и появление элементов панели управления."""
        try:
            WebDriverWait(self.driver, timeout_sec // 2).until(
                EC.invisibility_of_element_located((By.XPATH, "//*[self::button or @role='button'][contains(.,'Подключиться') or contains(.,'Присоединиться') or contains(.,'Запросить доступ') or contains(.,'Войти')]"))
            )
            logger.debug("Join button disappeared: likely joined")
        except Exception as e:
            logger.warning("Failed to detect disappearance of join button: %s", e)

        try:
            present_controls = self._element_present([
                ("css", "button[aria-label*='микрофон']"),
                ("css", "button[aria-label*='Микрофон']"),
                ("css", "button[aria-label*='камера']"),
                ("css", "button[aria-label*='Камера']"),
                ("xpath", "//button[contains(@aria-label,'Микрофон') or contains(@aria-label,'Камера')]"),
                ("xpath", "//button[.//span[contains(text(),'Выйти')] or contains(.,'Выйти')]"),
            ], timeout=timeout_sec // 2)
            if present_controls:
                logger.info("Meeting has been joined (Yandex)")
                return True
            else:
                logger.debug("Controls not found: meeting likely not joined")
        except Exception as e:
            logger.warning("Failed to detect presence of controls: %s", e)

        try:
            waiting = self._element_present([
                ("xpath", "//*[contains(.,'ожид') or contains(.,'Ожид') or contains(.,'ждут') or contains(.,'Запрос')]"),
                ("xpath", "//*[contains(.,'ожидание') or contains(.,'ожидайте') or contains(.,'ждём') or contains(.,'ждем')]"),
            ], timeout=5)
            if waiting:
                logger.info("Waiting for host to admit (Yandex)")
            else:
                logger.debug("No waiting message detected")
        except Exception as e:
            logger.warning("Failed to detect waiting message: %s", e)

        logger.warning("Meeting has not been joined (Yandex)")
        return False

    def wait_until_left(self, check_interval_sec: int = 5, max_wait_sec: Optional[int] = None) -> bool:
        """Ожидать окончания встречи/выхода: контролы исчезли или появился индикатор выхода.
        Возвращает True, когда встреча завершилась или мы вышли, False при истечении max_wait_sec.
        """
        start = time.time()
        logger.info("Waiting until meeting ends/left (Yandex)")
        while True:
            try:
                controls_present = self._element_present([
                    ("css", "button[aria-label*='микрофон']"),
                    ("css", "button[aria-label*='Микрофон']"),
                    ("css", "button[aria-label*='камера']"),
                    ("css", "button[aria-label*='Камера']"),
                    ("xpath", "//button[contains(@aria-label,'Микрофон') or contains(@aria-label,'Камера')]"),
                    ("xpath", "//button[.//span[contains(text(),'Выйти')] or contains(.,'Выйти')]"),
                ], timeout=2)
            except Exception:
                controls_present = False

            if not controls_present:
                left_indicator = self._element_present([
                    ("xpath", "//*[contains(.,'Вы покинули') or contains(.,'Встреча завершена') or contains(.,'Вы вышли') or contains(.,'закончена')]"),
                ], timeout=2)
                if left_indicator:
                    logger.info("Meeting ended/left indicator found (Yandex)")
                else:
                    logger.info("Controls disappeared — assuming meeting ended/left (Yandex)")
                return True

            if max_wait_sec is not None and (time.time() - start) > max_wait_sec:
                logger.warning("wait_until_left timeout reached (%ss)", max_wait_sec)
                return False

            time.sleep(check_interval_sec)

    def leave(self) -> None:
        """Покинуть встречу (при необходимости)."""
        try:
            left = self._click_first_available([
                ("xpath", "//button[.//span[contains(text(),'Выйти')]]"),
                ("xpath", "//button[contains(text(),'Выйти')]"),
                ("xpath", "//button[.//span[contains(text(),'Покинуть')]]"),
            ], timeout=5)
            if left:
                logger.info("Leave meeting: Done (Yandex)")
            else:
                logger.debug("Leave meeting: noop (Yandex)")
        except Exception:
            // Если сессия невалидна, просто игнорируем
            logger.debug("Leave meeting: skipped (invalid session)")
        // Отладочная информация
        try:
            logger.debug("Current URL: %s", self.driver.current_url)
        except Exception:
            pass

    def close(self) -> None:
        """Закрыть браузер и освободить ресурсы."""
        try:
            self.driver.quit()
        except Exception:
            pass