// CUSTOMER STORIES COVERED: CS-9.1 (localized Cognito login page — client-side behavior only)
//
// This spec runs in the "login" Playwright project (baseURL :5180) where the
// dev-auth bypass is NOT applied, so /login renders the real LoginPage. We use
// `page` directly (NO gotoApp fixture, NO WS mock) — there is no Cognito user
// pool here, so we exercise ONLY the client-side surface: rendering, the
// show/hide-password toggle, empty-field validation, and the language selector.
// We never attempt a real sign-in.
import { test, expect } from '@playwright/test';

test.describe('CS-9.1 login page (localized, client-side only)', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/login');
    // The login card renders with the product title.
    await expect(page.getByRole('heading', { name: 'AICC Builder' })).toBeVisible();
  });

  test('renders the Korean login form by default (ko-KR)', async ({ page }) => {
    // Title heading.
    await expect(page.getByRole('heading', { name: 'AICC Builder' })).toBeVisible();

    // Korean subtitle proves ko-KR is the default language.
    await expect(
      page.getByText('AI 컨택센터를 구축하려면 로그인하세요'),
    ).toBeVisible();

    // Email + password inputs by id.
    const email = page.locator('#email');
    const password = page.locator('#password');
    await expect(email).toBeVisible();
    await expect(email).toHaveAttribute('type', 'email');
    await expect(password).toBeVisible();
    await expect(password).toHaveAttribute('type', 'password');

    // The submit button (Korean "로그인" by default; bilingual to be safe).
    await expect(page.getByRole('button', { name: /^로그인$|^Sign In$/ })).toBeVisible();
  });

  test('show/hide password toggle flips the password input type', async ({ page }) => {
    const password = page.locator('#password');
    await password.fill('Sup3rSecret!');

    // Starts masked.
    await expect(password).toHaveAttribute('type', 'password');

    // The eye toggle is labelled "Show password" while masked.
    const showToggle = page.getByRole('button', { name: 'Show password' });
    await expect(showToggle).toBeVisible();
    await showToggle.click();

    // Now revealed → type flips to text, and the label flips to "Hide password".
    await expect(password).toHaveAttribute('type', 'text');
    const hideToggle = page.getByRole('button', { name: 'Hide password' });
    await expect(hideToggle).toBeVisible();

    // Click again → masked once more.
    await hideToggle.click();
    await expect(password).toHaveAttribute('type', 'password');
    await expect(page.getByRole('button', { name: 'Show password' })).toBeVisible();
  });

  test('submitting empty fields shows a client-side validation alert (no Cognito call)', async ({ page }) => {
    // Ensure fields are empty, then submit the form via the Sign In button.
    await expect(page.locator('#email')).toHaveValue('');
    await expect(page.locator('#password')).toHaveValue('');

    await page.getByRole('button', { name: /^로그인$|^Sign In$/ }).click();

    // The aria-live alert surfaces the "enter email and password" message.
    const alert = page.getByRole('alert');
    await expect(alert).toBeVisible();
    await expect(alert).toHaveText(/이메일과 비밀번호를 입력|Please enter email and password/);
  });

  test('language selector switches the subtitle to English (en-US)', async ({ page }) => {
    // Korean subtitle is shown first.
    await expect(page.getByText('AI 컨택센터를 구축하려면 로그인하세요')).toBeVisible();

    // The page exposes a <select aria-label="Language"> with language-code values.
    const langSelect = page.getByRole('combobox', { name: 'Language' });
    await expect(langSelect).toBeVisible();
    await langSelect.selectOption('en-US');

    // Subtitle switches to English; the Korean one disappears.
    await expect(page.getByText('Sign in to build your AI Contact Center')).toBeVisible();
    await expect(page.getByText('AI 컨택센터를 구축하려면 로그인하세요')).toHaveCount(0);

    // The submit button label also localizes to English.
    await expect(page.getByRole('button', { name: 'Sign In' })).toBeVisible();
  });
});
