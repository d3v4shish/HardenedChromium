// Copyright 2020 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "components/performance_manager/freezing/freezer.h"

#include "base/test/scoped_feature_list.h"
#include "components/performance_manager/public/performance_manager.h"
#include "components/performance_manager/test_support/performance_manager_test_harness.h"
#include "components/performance_manager/test_support/test_harness_helper.h"
#include "content/public/browser/browser_context.h"
#include "content/public/browser/browser_task_traits.h"
#include "content/public/browser/browser_thread.h"
#include "content/public/browser/web_contents.h"
#include "content/public/test/mock_permission_controller.h"
#include "content/public/test/web_contents_tester.h"
#include "testing/gtest/include/gtest/gtest.h"
#include "third_party/blink/public/common/features.h"
#include "url/gurl.h"

namespace performance_manager {
namespace mechanism {

namespace {

static constexpr char kUrl[] = "https://www.foo.com/";

void MaybeFreezePageNode(content::WebContents* content) {
  base::WeakPtr<PageNode> page_node =
      PerformanceManager::GetPrimaryPageNodeForWebContents(content);
  ASSERT_TRUE(page_node);
  Freezer freezer;
  freezer.MaybeFreezePageNode(page_node.get());
}

void UnfreezePageNode(content::WebContents* content) {
  base::WeakPtr<PageNode> page_node =
      PerformanceManager::GetPrimaryPageNodeForWebContents(content);
  ASSERT_TRUE(page_node);
  Freezer freezer;
  freezer.UnfreezePageNode(page_node.get());
}

}  // namespace

class FreezerTest : public PerformanceManagerTestHarness {
 public:
  FreezerTest() {
    feature_list_.InitAndDisableFeature(
        blink::features::kHardenedPrivacyMode);
  }

 protected:
  void EnableHardenedPrivacyMode() {
    feature_list_.Reset();
    feature_list_.InitAndEnableFeature(
        blink::features::kHardenedPrivacyMode);
  }

 private:
  base::test::ScopedFeatureList feature_list_;
};

TEST_F(FreezerTest, FreezeAndUnfreezePage) {
  SetContents(CreateTestWebContents());

  content::WebContentsTester* web_contents_tester =
      content::WebContentsTester::For(web_contents());
  EXPECT_TRUE(web_contents_tester);
  web_contents_tester->NavigateAndCommit(GURL(kUrl));

  web_contents()->WasHidden();

  MaybeFreezePageNode(web_contents());
  EXPECT_TRUE(web_contents_tester->IsPageFrozen());

  UnfreezePageNode(web_contents());
  EXPECT_FALSE(web_contents_tester->IsPageFrozen());
}

TEST_F(FreezerTest, CantFreezePageWithNotificationPermission) {
  SetContents(CreateTestWebContents());

  // Allow permissions.
  GetBrowserContext()->SetPermissionControllerForTesting(
      std::make_unique<content::MockPermissionController>());

  NavigateAndCommit(GURL(kUrl));

  // Try to freeze the page node, this should fail.
  MaybeFreezePageNode(web_contents());
  EXPECT_FALSE(content::WebContentsTester::For(web_contents())->IsPageFrozen());
}

TEST_F(FreezerTest, HardenedPrivacyModeDoesNotFreezePage) {
  EnableHardenedPrivacyMode();
  SetContents(CreateTestWebContents());
  content::WebContentsTester* web_contents_tester =
      content::WebContentsTester::For(web_contents());
  web_contents_tester->NavigateAndCommit(GURL(kUrl));
  web_contents()->WasHidden();

  MaybeFreezePageNode(web_contents());

  EXPECT_FALSE(web_contents_tester->IsPageFrozen());
}

}  // namespace mechanism
}  // namespace performance_manager
